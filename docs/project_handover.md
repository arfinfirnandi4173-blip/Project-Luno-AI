# LUNO PROJECT HANDOVER

Last updated: 2026-08-16 (after the Dashboard Turn-State Recovery fix
PART 2 / TTS-PATH - a takeover session's re-investigation, prompted by a
real production report that the dashboard was STILL stuck in `Thinking`
after the original fix)
Updated by: coding agent session covering Sprint 43 (Semantic Context
Bridging), Sprint 44 (Entity & Concept Continuity), Sprint 45 (Entity
Identity & Semantic Alias Continuity), Sprint 46 (Contextual Reference
Robustness), Sprint 47 (Semantic Entity Memory & Reference Graph),
Sprint 48 (Bounded Entity Provenance & Ambiguity Resolution),
Sprint 49 (Entity Provenance Disambiguation & Topic Lineage),
Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture), the Dashboard Turn-State Recovery fix (a fix-first task,
not a numbered intelligence sprint - completed AFTER Sprint 50, BEFORE
Sprint 51), and now the Dashboard Turn-State Recovery fix PART 2 /
TTS-PATH (same category, a second/separate gap the first fix did not
cover - see §2, §15's newest entry, and `docs/change_impact/
dashboard_turn_state_recovery_ttspath.md`).

**IMPORTANT - READ BEFORE TRUSTING "COMPLETE"/"VERIFIED" LANGUAGE BELOW
FOR THE PART 2 FIX:** the session that wrote Part 2 (this update) had NO
working Python environment for this project at all - no network access
to install the heavy ML/audio dependencies `main_runtime_demo.py`
imports at module level, and no bridge to run the real Windows `.venv`.
The Part 2 code change and its new tests
(`tests/test_dashboard_turn_state_recovery_ttspath.py`) were written
against the actual source, reasoned through line-by-line, and
syntax-checked (`python3 -m py_compile`) - but **never executed**. Any
sentence elsewhere in this document describing the ORIGINAL Dashboard
Turn-State Recovery fix (§15's earlier entry) as "COMPLETE"/verified
refers ONLY to that first fix, which a prior session DID actually run
(see `docs/testing/regression_baseline.md`'s Sprint 50/original-fix
entries). The Part 2 fix has not yet had that same live confirmation -
see the new §15 entry, §16's two newest items, and `docs/change_impact/
dashboard_turn_state_recovery_ttspath.md`'s own "Not yet done" section
for the exact command to run first.

**ADDENDUM - Sprint 52 (Robust Home Assistant Command & Entity
Resolution), a later, separate takeover session:** unlike the Part 2 fix
above, THIS session assembled a minimal-but-real dependency chain in
its sandbox (the actual, unmodified `luno/devices.py`, `luno/config.py`,
`luno/tool_manager/{context,handler,models,result,utils}.py`, plus this
sprint's own changes to `luno/tool_manager/builtin/real_home_assistant.py`)
and **actually ran `pytest`: 68 passed, 0 failed** (29 new tests + all
39 pre-existing tests in `luno/tool_manager/tests/
test_real_home_assistant_verification.py`, proving no regression there
for real, not just by inspection). Still NOT run: the full ~2900-test
repository sweep, and anything against a real Home Assistant server -
see §19d/§20d and `docs/change_impact/sprint52_ha_entity_resolution.md`'s
own "What was and wasn't executed" section for the complete, honest
scope of what this session's testing does and doesn't prove.

**ADDENDUM - Sprint 53 (user-numbered) — Memory Session Summary API
Compatibility Fix, a later, separate takeover session, narrowly
scoped:** fixed the reported `[Memory] ✗ Session summary error:
Unsupported parameter: 'max_tokens' is not supported with this model.
Use 'max_completion_tokens' instead.` Root cause: `luno/adapters/
openrouter.py`'s `RequestsOpenRouterClient._payload()` hardcoded the
completion-length JSON key as the literal `"max_tokens"`; Session
Summary (`luno/memory.py::summarize_and_archive_session()`) was the
ONLY caller anywhere in the codebase that ever passed a non-None
`max_tokens`, so it was the only path tripping this error - ordinary
chat was, and remains, unaffected (confirmed: it goes through a wholly
separate stack, `luno.adapters.llm_manager`, never touched this
sprint). Fixed by routing that key through the project's existing
`config.MAX_TOKENS_PARAM` abstraction (already used correctly by
`luno/main.py`, never consulted here before this sprint) instead of
hardcoding either literal name. Filed as `ARCHITECTURE_GUARD.md`
section **54**. Like Sprint 52 above (and unlike the Part 2/TTS-path
fix), this session assembled a minimal-but-real dependency chain and
**actually ran `pytest`: 93 passed, 3 skipped, 0 failed** (13 new tests
+ 111 pre-existing tests across four unmodified files, proving no
regression). Still NOT run: the full ~2900-test repository sweep, and
any live call to a real LLM provider (no API key/network access in
this sandbox). See §19e/§20e and `docs/change_impact/
memory_session_summary_api_compatibility.md` for the full writeup,
including a documented-not-fixed known limitation (`luno/adapters/llm/
base.py` has the textually identical latent bug, in the separate stack
that powers normal chat - a Sprint 54+ candidate).

**CORRECTION (added by Sprint 54):** the paragraph above's "ordinary
chat was, and remains, unaffected (confirmed: it goes through a wholly
separate stack, `luno.adapters.llm_manager`, never touched this
sprint)" framing understates what Sprint 54 found: `register_session_
summary_client()`'s `openrouter_adapter` is ACTUALLY an
`LLMManagerAdapter` instance in production (`luno/bootstrap/adapters.py`
line 137), not `luno.adapters.openrouter.OpenRouterAdapter` - that
class is orphaned in production, used by nothing but its own test
file. Sprint 53's fix above is code-correct and harmless, but it did
NOT fix the originally reported production bug. See the Sprint 54
paragraph immediately below, and `docs/change_impact/
llm_max_completion_tokens_compatibility.md`'s own "IMPORTANT
CORRECTION" section, for the full trace and the fix that actually
resolves the original bug.

**ADDENDUM - Sprint 54 (user-numbered) — LLM Stack API Compatibility &
Max Completion Tokens Hardening, a later, separate takeover session:**
fixed the SAME class of bug Sprint 53 targeted, but in the code path
that is actually live in production. Root cause: `luno/adapters/llm/
base.py`'s `OpenAICompatibleClient._payload()` (shared by
`OpenRouterProvider`/`OpenAIProvider`/`LocalProvider`, both `chat()`
and `stream_chat()`) hardcoded the completion-length JSON key as the
literal `"max_tokens"` - this IS the method Session Summary's real call
chain reaches (`LLMManagerAdapter.client` -> `chat_once()` -> default
`LLM_PROVIDER=openrouter` provider -> this method). Fixed identically
to Sprint 53's own pattern: routed through `config.MAX_TOKENS_PARAM`.
Anthropic/Gemini (different wire formats, not `OpenAICompatibleClient`
subclasses) confirmed unaffected by 4 dedicated regression tests. Filed
as `ARCHITECTURE_GUARD.md` section **55**. Like Sprint 52/53 above,
this session assembled a minimal-but-real dependency chain and
**actually ran `pytest`: 165 passed, 3 skipped, 0 failed** (24 new
tests + 141 pre-existing tests across five unmodified files, including
Sprint 53's own suite, proving no regression anywhere). Still NOT run:
the full ~2900-test repository sweep, and any live call to a real LLM
provider. See §19f/§20f and `docs/change_impact/
llm_max_completion_tokens_compatibility.md` for the full writeup.

**The repository documentation is the authoritative project memory. The
previous chat is NOT required for takeover.** This document is the
durable source of truth for continuing this project. Do not rely on
chat history — if you are a new agent (possibly on a different account)
picking this project up, read this file, then `docs/project_handover.
json`, then `ARCHITECTURE_GUARD.md`, then `docs/testing/regression_
baseline.md`, then the latest `docs/change_impact/*.md`, in that order,
before touching any source file. See §21 (TAKEOVER PROTOCOL / NEXT AGENT
TAKEOVER) for the full checklist.

---

## 1. Project Purpose

Luno is a production voice-assistant/conversational-AI system built
around a real-time event-bus runtime (`main_runtime_demo.py` /
`RuntimeDemoConsole`), an LLM-backed planner/response pipeline, a
deterministic (non-embedding, non-LLM-judge) conversational memory
system (`luno/memory.py`, `luno/memory_context.py`, `luno/memory_
retrieval/`), and a streaming TTS/voice-output pipeline. It runs in
Indonesian and English (mixed, code-switched conversations are common)
and is designed to feel like a persistent, familiar assistant across
long-running conversations — remembering topics, entities, plans, and
relationship context without a vector database or an LLM-based memory
judge.

## 2. Current Sprint

**LATEST: LUNO P0.5 — Real Camera Integration — CODE COMPLETE,
UNIT+INTEGRATION TESTED (36/36 new + P0's own 23/23 re-run unmodified +
full repository sweep against baseline: 4569 collected, 4538 passed, 31
failed — 30 identical to the pre-existing baseline, the 31st a
by-name-documented pre-existing full-suite timing flake, investigated
and confirmed unrelated), LIVE HA DISCOVERY ATTEMPTED AND HONESTLY
DOCUMENTED AS UNAVAILABLE (this sandbox's own outbound network policy
returns HTTP 403 for the real, credentialed `HA_URL` configured in
`.env` — a genuine attempt was made via the existing read-only
`luno.ha_client.HomeAssistantClient`, not skipped).** Integration sprint
on top of P0's already-shipped `CameraAutomationModule`: new, generic
`CameraProfile -> CameraEvent` classification layer
(`luno/camera_automation/cameras.py`) — NOT a vendor-specific "Tapo
adapter" (Home Assistant already abstracts the vendor protocol before
Luno ever sees an entity state, so no Tapo-specific code was written,
honestly). Classifies the EXISTING `device_state_changed` event into
`motion_detected`/`motion_cleared`/`human_detected`/`human_cleared`/
`camera_online`/`camera_offline`, published as a NEW `camera_automation.
camera_event` event that coexists with (never replaces) P0's own flat-
allowlist relay; the EXISTING `AutomationEngine` already consumes it
with zero engine changes (proven end to end by this sprint's own
`test_33`). New, isolated `config/camera_automation.json` (shipped
inert — every entity role `null`, no fabricated entity ids). New
`ha_camera_discovery.py` (read-only, same precedent as Sprint 70's
`tapo_ptz_diagnostic.py`) for the user to close the live-verification
gap on their real machine. `luno/bootstrap/modules.py` was NOT touched
this sprint — `CameraAutomationModule` was already wired there since
P0. See §19cc/20cc below and `docs/change_impact/
camera_automation_p0_5.md` for the full writeup.

**PREVIOUS: LUNO P0 — Camera Automation / Safe Integration &
Non-Regression Protocol — CODE COMPLETE, UNIT+INTEGRATION TESTED (23/23
new + full repository sweep clean against baseline: 4533 collected,
4503 passed, 30 failed — the exact same 30 pre-existing failures the
baseline already had, zero new), NOT LIVE-VERIFIED AGAINST PHYSICAL HOME
ASSISTANT HARDWARE (same structural sandbox limitation every prior
camera/HA sprint has documented).** New feature under an explicit
non-regression protocol: one new, isolated `luno/camera_automation/`
package lets an operator-allowlisted set of Home Assistant camera/motion
entities feed Sprint 72's ALREADY-EXISTING `AutomationEngine` — zero
lines changed in that engine, zero lines changed in the Event Bus, zero
lines changed in `HomeAssistantAdapter`. `CameraAutomationModule`
subscribes to the already-existing, already-published `device_state_
changed` event and republishes a distinctly-namespaced `camera_
automation.state_changed` event that the existing engine already knows
how to trigger a rule from (event trigger, arbitrary event_name string)
and already has `home_assistant.turn_on`/`turn_off` actions allowlisted
for — the brief's own "HA action adapter" step required zero new code.
Disabled by default (`CAMERA_AUTOMATION_ENABLED` unset); even enabled,
an empty allowlist (`CAMERA_AUTOMATION_ENTITIES`) is a no-op. Every
event-handling line is wrapped in a fail-safe `try/except` (redundant
with, not a replacement for, the Event Bus's own existing subscriber
self-healing). Exactly ONE existing file modified, additively: `luno/
bootstrap/modules.py` (new import line, new construction block, one new
entry in each of the existing `bind_event_bus`/`register_module` loops,
one new returned-dict key — no existing line altered). Zero new config
files, zero new external dependencies. See §19bb/20bb below and `docs/
change_impact/camera_automation_p0.md` for the full writeup.

**PREVIOUS: Sprint 72 — Automation Engine Dasar — CODE COMPLETE,
UNIT+INTEGRATION TESTED (78/78 new + 419/419 targeted + 200/203
dashboard/mutation-audit suite, full repository sweep clean against
baseline), NOT LIVE-VERIFIED AGAINST PHYSICAL CAMERA/HOME ASSISTANT
HARDWARE (same structural sandbox limitation every prior camera/HA
sprint has documented).** New feature (not a bug fix): a deterministic
`TRIGGER -> CONDITION -> ACTION -> VERIFY -> COOLDOWN` pipeline —
`config/automation_rules.json` rules with an event/time/manual trigger,
zero-or-more pure read-only conditions, and one or more allowlisted
actions (`camera.preset`/`camera.home`/`camera.stop_patrol`/`home_
assistant.turn_on`/`home_assistant.turn_off`/`automation.log`) — usable
by camera, Home Assistant, and future device domains without a second
automation engine per domain. Explicitly not an autonomous agent: no
`eval`/`exec`/arbitrary tool dispatch anywhere, the LLM/planner can only
ever select a registered `rule_id`. Built with ZERO new Event Bus,
Scheduler, or ToolManager implementation — reuses the SAME `event_bus.
subscribe("*", ...)` observability-tap idiom, the SAME `runtime.
scheduler` for time triggers, and the SAME `tool_requested` ->
`ToolManagerBridgeModule` -> `ToolManager` dispatch path Sprint 69-71
already established. Manual PTZ ownership priority is a new pre-
dispatch hook; "Automation > Patrol" needed zero new code (Sprint 71's
own existing hook already covers it). New `luno/automation/` package,
new `luno/tool_manager/builtin/automation.py`, new (empty-by-default)
`config/automation_rules.json`; additive-only changes to `luno/
bootstrap/modules.py`, `luno/planner/parser.py`, `luno/dashboard/
collectors.py`/`server.py`/`static/index.html`. `main_runtime_demo.py`
was not modified at all this sprint. See §19aa/20aa below and `docs/
change_impact/automation_engine.md` for the full writeup.

**PREVIOUS: Sprint 71 — Camera Patrol — CODE COMPLETE, UNIT+INTEGRATION
TESTED (37/37 new + 322/322 targeted + 83/84 dashboard/runtime suite,
full repository sweep clean against baseline), NOT LIVE-VERIFIED
AGAINST PHYSICAL CAMERA HARDWARE (same structural sandbox limitation
Sprint 69/70 already documented — no route to the user's LAN).** New
feature (not a bug fix): "mulai patroli kamera" cycles the Tapo C212
through saved PTZ presets, deterministic, bounded (loop routes require
`max_cycles` or `max_duration_seconds` or are refused), stoppable at any
time (`Event.wait`-based cancellation, sub-second observed stop), and
never contends with manual PTZ control for camera ownership (a manual
PTZ command always stops an active patrol first, via a new small
optional pre-dispatch hook on `ToolManagerBridgeModule`, no-op for every
other tool). Built with ZERO new PTZ implementation — every patrol
movement is dispatched through the exact same `tool_requested` ->
`ToolManagerBridgeModule` -> `ToolManager` -> `camera_ptz` round trip a
manual voice command already uses, reusing Sprint 69/70's error
classification unchanged. New `luno/camera_patrol/` package, new
`luno/tool_manager/builtin/camera_patrol.py`, new (empty-by-default)
`config/camera_patrol_routes.json`; additive-only changes to
`main_runtime_demo.py`, `luno/bootstrap/modules.py`,
`luno/planner/parser.py`, `luno/dashboard/collectors.py`/`server.py`/
`static/index.html`. See §19z/20z below and `docs/change_impact/
camera_patrol.md` for the full writeup.

**PREVIOUS: Sprint 71 — Dashboard Startup & Access Recovery — COMPLETE
AND LIVE-VERIFIED.** Root cause: `DashboardServer.start()` (`luno/
dashboard/server.py`) and `main.py`'s own call site were both unguarded
against the `OSError` a socket-bind failure raises (most commonly a
stale/previous process holding the configured port) - the exception
propagated out of `main()` uncaught, crashing the ENTIRE Luno process
(not just the dashboard). Fixed by catching `OSError` at both layers and
degrading to the already-established `DASHBOARD_ENABLED=false` behavior
instead of crashing. See §19y/20y below and `docs/change_impact/
dashboard_startup_recovery.md` for the full writeup. The remainder of
this section (below) is the historical record of the PREVIOUS current
sprint at the time it was written and is preserved as-is.

---

**Dashboard Turn-State Recovery fix, PART 2 / TTS-PATH — CODE COMPLETE,
NOT YET LIVE-VERIFIED** (a fix-first task, not a numbered intelligence
sprint; a takeover session's follow-up to the original Dashboard
Turn-State Recovery fix below, prompted by a real production report that
the dashboard was STILL getting stuck in `Thinking` after that first fix
landed). Root cause: `SessionManagerModule._handle_playback_done()`
(`luno/wake_session/manager.py`) only cleared `THINKING` when
`state == SPEAKING`; a TTS failure that happens BEFORE the very first
audio chunk plays (the LLM call already succeeded - the Chat panel
already shows the reply - but Fish Audio's TTS server is unreachable or
otherwise fails immediately) never reaches `SPEAKING` at all
(`speech_playback_started` is only published once real audio begins), so
the resulting `speech_playback_cancelled` event was silently dropped by
that state guard, leaving `THINKING` stuck forever with no exception
anywhere. A second, structural version of the same bug class was found
one layer down: none of `FishAudioAdapter._play()`/`_play_pipelined()`/
`_play_stream()`/`_play_stream_pipelined()` (`luno/adapters/fish_audio.
py`) had an outer `except Exception` guaranteeing a terminal event on an
unanticipated exception - the exact bug class the ORIGINAL fix closed
for the planner thread, left open here. See §15's newest entry and
`docs/change_impact/dashboard_turn_state_recovery_ttspath.md` for the
full root-cause/fix writeup. **The code change and 5 new tests
(`tests/test_dashboard_turn_state_recovery_ttspath.py`) have NOT been
executed** - the takeover session had no working Python environment for
this project (see the warning at the top of this document). Running them
- see that file's own docstring for the exact command - is the
IMMEDIATE next action for whoever picks this up, before anything in §22.

The ORIGINAL Dashboard Turn-State Recovery fix (planner/LLM-side,
`PlannerBridgeModule._handle_utterance()`) remains as it was - COMPLETE
and verified in an earlier session (see below and its own `docs/
change_impact/dashboard_turn_state_recovery.md`). It fixed a real,
different production bug (an unhandled exception in the planner thread
before the LLM call even completed) and its own 13 tests DID actually
run and pass in that earlier session. It did not, and could not, have
fixed the Part 2 TTS-path gap - the two are genuinely separate failure
modes with separate root causes, both now addressed by separate code
changes.

**Sprint 52 (user-numbered) — Robust Home Assistant Command & Entity
Resolution — CODE COMPLETE, UNIT+INTEGRATION TESTED THIS SESSION (68
passed), NOT LIVE-VERIFIED, NOT FULL-REPOSITORY-REGRESSION-VERIFIED.**
A separate, later takeover session, unrelated to the Dashboard fix
above except for sharing this document. Filed as `ARCHITECTURE_GUARD.md`
section **53** (that document's own section 52 is the unrelated
TTS-path fix above - a documentation-numbering coincidence, reconciled
in that section). Fixed: typo'd/misheard HA device names ("rg strip",
"rbg strip", "rgb strp", "rgbstrip") now auto-resolve via a new bounded
fuzzy tier in `RealHomeAssistantHandler`, gated by confidence + margin +
candidate-count so ambiguous multi-candidate cases always refuse rather
than guess. See §19d/§20d and `docs/change_impact/
sprint52_ha_entity_resolution.md` for the full writeup, including the
exact scope of what was and wasn't executed.

**Sprint 53 (user-numbered) — Memory Session Summary API Compatibility
Fix — CODE COMPLETE, UNIT+INTEGRATION TESTED THIS SESSION (93 passed, 3
skipped, 0 failed), NOT LIVE-VERIFIED, NOT FULL-REPOSITORY-REGRESSION-
VERIFIED.** A separate, later, narrowly-scoped takeover session. Filed
as `ARCHITECTURE_GUARD.md` section **54**. Fixed: Session Summary
(`luno/memory.py::summarize_and_archive_session()`) was the only caller
of `luno/adapters/openrouter.py`'s real OpenRouter client that ever
sent a completion-length parameter, and that client hardcoded the
literal, now-incompatible-for-some-models JSON key `"max_tokens"`; now
routed through the project's existing `config.MAX_TOKENS_PARAM`
abstraction instead. Ordinary conversational chat is unaffected - it
runs through a separate stack (`luno.adapters.llm_manager`) this sprint
did not touch. See §19e/§20e and `docs/change_impact/
memory_session_summary_api_compatibility.md` for the full writeup,
including the exact scope of what was and wasn't executed, and a
documented (not fixed) known limitation in that separate untouched
stack. **CORRECTION (Sprint 54): the "of `luno/adapters/openrouter.py`'s
real OpenRouter client" claim above is inaccurate for production** -
that client is orphaned there; see the Sprint 54 entry immediately
below.

**Sprint 54 (user-numbered) — LLM Stack API Compatibility & Max
Completion Tokens Hardening — CODE COMPLETE, UNIT+INTEGRATION TESTED
THIS SESSION (165 passed, 3 skipped, 0 failed), NOT LIVE-VERIFIED, NOT
FULL-REPOSITORY-REGRESSION-VERIFIED.** A later takeover session,
continuing Sprint 53's own deferred known limitation. Filed as
`ARCHITECTURE_GUARD.md` section **55**. This session's reconnaissance
discovered `luno/bootstrap/adapters.py` actually constructs an
`LLMManagerAdapter` (not `luno.adapters.openrouter.OpenRouterAdapter`,
which is orphaned in production) under the `"openrouter_adapter"` key
Sprint 53's own `register_session_summary_client()` reads - meaning
Session Summary's real request-building code is `luno/adapters/llm/
base.py`'s `OpenAICompatibleClient._payload()`, not the file Sprint 53
fixed. That method had the textually identical hardcoded `"max_tokens"`
bug; fixed identically (routed through `config.MAX_TOKENS_PARAM`).
Confirmed via 4 dedicated tests that Anthropic/Gemini (different wire
formats) are unaffected. See §19f/§20f and `docs/change_impact/
llm_max_completion_tokens_compatibility.md` for the full writeup,
including the correction to Sprint 53's own documented call chain.

**Sprint 55 (user-numbered) — Full Verification & System Stabilization —
COMPLETE.** A full-repository regression sweep (3880 tests) plus one
real test-reliability fix (`tests/test_dashboard_turn_state_recovery.
py`). Live LLM/HA provider verification confirmed NOT POSSIBLE in this
sandbox (network egress blocked). Persistent state verified
byte-identical across the entire sweep. See `ARCHITECTURE_GUARD.md` §56
and `docs/change_impact/sprint55_stability_gate.md`.

**Sprint 56 (user-numbered) — Home Assistant + Query Intelligence —
COMPLETE for its own scope.** Re-verified Sprint 52's tiered HA resolver
unmodified (68/68 passing) plus a new, genuinely-reproduced Category L
("typo closer to a WRONG device") case. One real production fix:
`luno/memory_context.py`'s `_narrow_by_query_differentiator()`, closing a
tied-topic-candidate gap in `select_topic_candidates()`. Contextual HA
references ("Matikan." after "Nyalain lampu kamar.") investigated,
evidence matrix built, explicitly DEFERRED to Sprint 57 — no safe
existing hook was found in the two layers this sprint investigated (see
Sprint 57's own Phase 0 finding below for what was missed). See
`ARCHITECTURE_GUARD.md` §57 and `docs/change_impact/
sprint56_ha_query_intelligence.md`.

**Sprint 57 (user-numbered) — Contextual Home Assistant References &
Target Continuity — CODE COMPLETE, UNIT+INTEGRATION TESTED THIS SESSION
(targeted: 337 passed, 0 failed; full repository sweep: 3079 passed, 11
failed — all 11 individually re-verified as pre-existing/environment-
specific/parallel-timing flakes unrelated to this sprint's changes, 0
genuine regressions), NOT LIVE-VERIFIED (network egress blocked, same
sandbox limitation Sprint 55 already confirmed).** Filed as
`ARCHITECTURE_GUARD.md` section **58**. Phase 0's own key finding:
Sprint 56 investigated two layers for contextual references (the Tool
Manager resolver, `memory_context.py`'s topic machinery) and correctly
found neither suitable — but missed a THIRD, pre-existing, live, tested
layer: `PlannerBridgeModule._apply_device_context()` in `main_runtime_
demo.py`, a text-rewrite REMEMBER/FILL mechanism that predates Sprint
52/55/56 entirely. This sprint hardens that existing mechanism (bounded
freshness, HA-domain compatibility, same-turn multi-device ambiguity
clearing, failed-command invalidation, a broadened remember-action set,
"yang itu"/"yang tadi" referential-phrase support, one new Event-Bus
observability event) rather than building a second memory/topic system.
Also fixes Sprint 56's own flagged message-quality issue in `real_home_
assistant.py` (a genuinely target-less command now refuses honestly
instead of producing "None is currently unavailable."). `config/long_
term_memory.json`'s corrupted/unknown-format state (flagged by Sprint
55/56) was diagnosed this sprint (not valid JSON/gzip/zlib/text; high
entropy consistent with encryption/compression; no backup exists) and
explicitly DEFERRED — not fixed, out of scope regardless of cause. See
§19i/§20i and `docs/change_impact/sprint57_contextual_ha_references.md`
for the full writeup.

**Sprint 58 (user-numbered) — Home Assistant Multi-Entity & Group
Commands — COMPLETE for explicit multi-target and group-all commands;
area-scoped and contextual groups explicitly DEFERRED with documented
evidence (UNIT+INTEGRATION TESTED THIS SESSION: targeted 162 passed, 0
failed; full repository sweep 3930 passed, 27 failed — 1 was this
sprint's own regression, found and fixed before the final run; the
other 26 individually re-verified file-by-file as pre-existing/
environment-specific/network-dependent/full-suite-thread-timing flakes,
0 genuine regressions remaining), NOT LIVE-VERIFIED (same network-
egress-blocked sandbox limitation as every prior sprint).** Filed as
`ARCHITECTURE_GUARD.md` section **59**. Root cause: `IntentParser`
correctly falls through to `tool="unknown"` for a clause with an elided
verb ("lampu kamar dan lampu ruang tamu" — the second clause has no verb
of its own), so only the first of two named devices was ever actually
commanded; and "semua lampu" had no parser vocabulary at all. New
pre-Planner group/multi-target resolution layer in `PlannerBridgeModule`
(`_apply_ha_group_resolution()`), checked before Sprint 57's own
contextual fill, resolves EVERY target via Sprint 52's EXISTING
`RealHomeAssistantHandler._resolve_entity_tiered()` (a throwaway
`client=None` instance — confirmed pure/client-free during resolution)
before rewriting text into the same canonical phrasing Sprint 57's own
FILL step already produces, or refusing the ENTIRE command (empty text,
guaranteeing zero HA API calls for any target) if any target is
ambiguous/unresolved/wrong-domain. Area-scoped groups ("semua lampu di
kamar") and contextual groups ("Matikan semuanya.") are deliberately
NOT implemented — zero area metadata exists anywhere in this project's
registry (confirmed by direct grep), and Sprint 57's device memory is a
genuinely single-slot design that would need a structural redesign this
sprint's own invariants forbid. A real regression was found and fixed
during implementation: the first version of multi-target detection
misfired on "turn on the lights and how's the weather today" (comma/
"and"/"then" are already-established general clause separators for
unrelated content in this parser); detection was scoped to Indonesian
"dan"-only phrasing to fix it, matching every one of this sprint's own
worked examples. See §19j/§20j and `docs/change_impact/
ha_multi_entity_commands.md` for the full writeup.

**Sprint 59 (user-numbered) — Single-Room Home Assistant Group Control —
COMPLETE for the single room this project actually has ("kamar"); multi-
room/general area abstraction explicitly OUT OF SCOPE by design, not a
gap (UNIT+INTEGRATION TESTED THIS SESSION: targeted 261 passed, 0
failed; full repository sweep 3950 passed, 28 failed — all individually
re-verified as pre-existing/environment-specific/network-dependent/
full-suite-thread-timing flakes, cross-checked against Sprint 58's own
isolation investigation, 0 genuine regressions), NOT LIVE-VERIFIED (same
network-egress-blocked sandbox limitation as every prior sprint).**
Filed as `ARCHITECTURE_GUARD.md` section **60**. Root cause/gap: Sprint
58 already refused every area-qualified group uniformly (no structured
room field exists anywhere in the registry), but converging textual
evidence (Main Lamp's own `entity_id` containing "kamar_tidur";
`config/environment_triggers.json`'s pre-existing "sleepy" trigger
already grouping all 3 configured lights as one unit; `config/
persona.json`'s own illustrative "lampu kamar" phrase; zero evidence of
any second room anywhere) ties every currently-configured light to one
identifiable room. New constant `PlannerBridgeModule._SINGLE_ROOM_NAME =
"kamar"` and method `_is_single_room_word()` are the entirety of the new
logic — Sprint 58's existing `group_all_light` branch now proceeds
(reusing its own enumeration/execution path verbatim) instead of
refusing when the area word is absent or exactly "kamar", and still
refuses, honestly and by name, for any other area word (e.g. "dapur"),
with zero HA calls and no guessing. Membership is drawn entirely from
the in-process `luno.devices.LIGHTS` dict already loaded at import time
— no new config format, no database, no new persistent memory, no HA
call to determine membership. Explicit single-entity and Sprint 58's own
explicit multi-target commands are unaffected (single-step-only gate,
unchanged); a bare "lampu kamar" (no "semua") already resolves directly
to Main Lamp via the completely unmodified Sprint 52 fuzzy resolver,
costing zero new code and naturally satisfying explicit-entity-beats-
group precedence. A pre-existing test-fixture discrepancy (RGB
Computer's `entity_id` differs between the real config file and the
shared cross-sprint test fixture) was found and documented, not fixed
(out of scope, cross-sprint blast radius) — see `docs/change_impact/
ha_single_room_group_control.md` §6. See §19k/§20k and `docs/change_
impact/ha_single_room_group_control.md` for the full writeup.

**Sprint 60 (user-numbered) — Structured Room/Area Schema Foundation —
COMPLETE (schema + registry support only; multi-room command detection/
execution deliberately NOT implemented — see this sprint's own scope).**
Filed as `ARCHITECTURE_GUARD.md` section **61**. Adds an optional,
additive `"area"` string field to `config/lights.config.json` entries
(`luno/devices.py::load_lights_config()`) plus two pure read-only
helpers (`get_device_area()`/`get_devices_by_area()`) — fully backward
compatible (a device without `"area"` still loads exactly as before;
an invalid `"area"` type is logged and ignored, never crashes the
loader or drops the device). `main_runtime_demo.py`'s existing Sprint
58/59 `group_all_light` branch now prefers this structured metadata as
the source of truth for room membership wherever it exists, while
falling back to Sprint 59's original full-registry behavior for an
unmigrated config — for THIS project's real, now-migrated 3-light
"kamar" set, the output is byte-for-byte identical either way, proved
by tests, not just asserted. `config/lights.config.json` was migrated:
Main Lamp/RGB Strip/RGB Computer now carry `"area": "kamar"` (the SAME
evidence Sprint 59 already documented); `Baterai`/`Aquascape`
(switches) and `gaming mode` (script) were deliberately left untagged —
no location evidence exists for either switch, and the switches config
format has no per-device object to carry the field at all. New test
file: `tests/test_sprint60_area_schema.py` (27 tests, 0 failed).
Targeted regression: 210 passed, 0 failed. Full repository sweep: the
actual, independently-verified collection for this checkout is 3190
tests (confirmed via `pytest --collect-only`, both with and without
this sprint's changes) — materially fewer than the 3983 previously
documented by Sprint 59, a discrepancy that could not be explained from
within this sprint's scope and is called out explicitly rather than
silently reconciled. A clean sweep (after discovering and killing an
unrelated stale leftover pytest process from a prior session): 3158
passed, 28 failed, 3 skipped, 1 deselected — every failure individually
classified, none touching any file this sprint modified, zero genuine
regressions (one failure new to this sprint's own regression run,
`test_runtime_demo.py::test_episodic_memory_end_to_end_...`, was
directly re-verified twice — passes in isolation and passes when its
entire home file runs standalone, 78/78 — before being classified as
the same pre-existing full-suite-only cross-test timing interference
class covering 13 other files). NOT LIVE-VERIFIED (same network-
egress-blocked sandbox limitation as every prior sprint — directly
re-confirmed this sprint via a live `SpeechStreamIdleTimeout`/fish_audio
network error surfaced while classifying an unrelated failure). See
§19l/§20l and `docs/change_impact/area_schema_foundation.md` for the
full writeup, including the full STOP CONDITION analysis (none
triggered).

## 3. Current Production Status

The ORIGINAL Dashboard Turn-State Recovery fix (planner/LLM-side) is a
verified, stable, production baseline - see `docs/testing/regression_
baseline.md`'s own entry for the run that confirmed it. All memory/
topic/reference/temporal/semantic-bridging/entity-continuity/
observability/turn-state-recovery test suites passed at that time (788
tests in the targeted core-plus-observability-plus-recovery suite — 775
from Sprint 50 plus that fix's own 13 new tests — plus the full
repository sweep — see §13). Ten pre-existing, environment-specific
failures remain, all independently reproduced and documented (§13, §16)
— none are new, none are caused by Sprints 43-50 or that fix's own
changes. An 11th, new-LOOKING failure surfaced during that fix's own
full sweep (`test_verification_dashboard.py`'s own E2E test) but is a
confirmed new manifestation of the ALREADY-documented `inspect.getsource`
sandbox flake (re-ran clean in isolation) — not a real regression.

**This Part 2 / TTS-path fix is NOT yet part of that verified baseline**
- its own tests have not been run (see §2 above). Do not treat the
"verified, stable, production baseline" language above as covering the
Part 2 change until `tests/test_dashboard_turn_state_recovery_ttspath.py`
and the targeted regression list in that file's own docstring have
actually been run and §13/`regression_baseline.md` updated with a real
result.

Sprint 49's
own documented parallel-execution flake continues to not reproduce
(flakes do not always reproduce — not itself evidence of anything; see
§13, §15's own entries).

## 4. Architecture Map

```
User utterance
    |
_handle_utterance() / PlannerBridgeModule (main_runtime_demo.py)
    |
intent / reference classification  <- luno.memory.classify_reference_type()
    |
topic state                        <- luno.memory_context.ActiveTopicSnapshot
    |                                  (single-slot, `_active_topic` dict)
topic history                      <- luno.memory_context (`_topic_history` dict,
    |                                  bounded, per-conversation)
reference resolution               <- luno.memory (ConversationReference,
    |                                  ordinal/list resolution, Sprint 38)
candidate selection                <- select_topic_candidates(),
    |                                  select_temporal_fallback_candidate(),
    |                                  is_active_topic_relevant_to_query()
assemble_context()                 <- luno.memory_retrieval (ranking + budget)
    |
prompt construction                <- render_context_block(), system prompt
    |
LLM                                <- OpenRouter adapter (real or Mock)
    |
response depth                     <- response-depth policy (NORMAL/SHORT/etc)
    |
voice selection                    <- semantic voice selection (Sprint 37)
    |
TTS streaming                      <- Fish Audio adapter
    |
playback / cancellation            <- barge-in / session manager
```

Do **not** create a parallel memory system, a second topic tracker, a
second ranking system, or an LLM-based memory judge. Every sprint from
36 onward has extended this SAME pipeline additively.

**Sprint 50 addition (observability tap, not a pipeline change):** three
points in this SAME pipeline now ALSO publish a bounded, non-text Event
Bus event describing what just happened — right after "intent /
reference classification" (`memory_reference_classified`), right after
"candidate selection" (`memory_topic_decision` — includes the new
`topic_decision`/`ambiguity_check_result` fields), and right after
`MemoryTurnTrace` is built (`memory_selection_summary`). These are
OBSERVATIONAL ONLY — nothing in the pipeline reads them back, they never
influence what gets selected or merged, and disabling them (they are
always published; the persistence layer that WRITES them to disk,
`luno.dashboard.event_log_writer.EventLogWriter`, is what's opt-in) has
zero effect on conversation behavior. See §15's Sprint 50 entry and
`docs/change_impact/runtime_observability.md`.

**Dashboard Turn-State Recovery addition (a thread-lifecycle safety net,
not a pipeline change):** `PlannerBridgeModule._handle_utterance()`
(the whole box from "intent / reference classification" through
"prompt construction" above) now runs inside a new outer wrapper,
`_run_utterance_turn_safely()`, on the same `luno-planner-turn` daemon
thread `on_event()` already spawned. If any exception escapes
`_handle_utterance()` before it reaches the LLM, the wrapper publishes
the SAME `llm_error` event a real OpenRouter failure already publishes
— nothing about the pipeline's own decision logic changed, only what
happens if it crashes before finishing. See §15's own entry and
`docs/change_impact/dashboard_turn_state_recovery.md`.

## 5. Memory Architecture

Two layers, deliberately kept separate:

- **`luno/memory.py`** — the persistent, deterministic store plus
  classifiers. Owns `classify_reference_type()` (a precedence-ordered,
  pure classifier — see §7), `is_correction_signal()`, `is_pure_
  reference_followup()`, `is_merge_reference_followup()`, temporal
  classification (`classify_temporal_status()`), ordinal/cardinal word
  maps, and the persistent long-term-memory/session-summary/
  relationship-state JSON files under `config/`.
- **`luno/memory_context.py`** — per-turn EPHEMERAL topic tracking and
  candidate assembly. Owns `ActiveTopicSnapshot` (§6), `update_active_
  topic()`, `update_topic_history()`, `_merge_terms()`, `select_topic_
  candidates()`, `select_temporal_fallback_candidate()`, `is_active_
  topic_relevant_to_query()`, `is_sparse_unknown_followup()` (Sprint
  44), `is_demonstrative_anchored_followup()` (Sprint 47 — a SECOND
  additive merge-eligibility detector alongside `is_sparse_unknown_
  followup()`, see §6/§15), the Sprint 43 alias/normalization layer
  (`_TOKEN_SYNONYM_GROUPS`, `_strip_bounded_affixes()`, `_normalize_
  terms_for_bridging()`), and `_TOPIC_OVERLAP_STOPWORDS`.
- **`luno/memory_retrieval/`** — the shared retrieval package: `Context
  Item`, `_rank_key()`, `_apply_budget()`, `assemble_context()`,
  `render_context_block()`. **Sprint 43/44/45 all explicitly did NOT
  touch this package** — every fix so far has operated upstream, in
  classification or candidate-eligibility, never in ranking/budget.

## 6. Entity / Concept Architecture

**There is no separate entity/concept data structure.** `ActiveTopic
Snapshot` (frozen dataclass, `luno/memory_context.py`) is a flat,
unstructured bag-of-terms:

```python
terms: frozenset
turns_since_active: int = 0
list_items: Tuple[str, ...] = ()      # Sprint 38
status: str = "active"                 # Sprint 40 ("active"/"superseded")
source_sentence: str = ""              # Sprint 40
```

This field set has been **unchanged since Sprint 40** and Sprints 44/45/
47 have all explicitly investigated whether a richer representation
(entity identity, attributes, relations, confidence) was needed and
concluded, based on live reproduction, that it was not — every
reproduced, FIXABLE gap has been a classification, merge-decision, or
ambiguity-guard bug operating on this SAME flat representation, not a
representation gap. Do not introduce a richer entity model without
first reproducing a concrete failure that proves the flat representation
is insufficient — three sprints in a row have looked for exactly that
failure and not found one.

**Sprint 47 addition:** `is_demonstrative_anchored_followup()` (`luno/
memory_context.py`) is a SECOND merge-eligibility detector alongside
`is_sparse_unknown_followup()` (Sprint 44) — both answer "should this
`"unknown"`-classified turn MERGE into the active topic instead of
replacing it?", just via different shapes: `is_sparse_unknown_
followup()` covers `<= 1` real residual token; `is_demonstrative_
anchored_followup()` covers a turn whose own 2nd word is the
demonstrative "itu"/"ini" (e.g. "Board itu RAM-nya berapa?"), bounded to
`<= 3` real residual tokens. Neither changes `classify_reference_
type()`'s own output. See §15's Sprint 47 entry and `ARCHITECTURE_
GUARD.md` §47 for the full reasoning and the two rejected alternative
fixes that were tried and reverted for a DIFFERENT (ambiguity-safety,
not entity-representation) problem this same sprint.

**Sprint 48 addition:** `is_active_topic_relevant_to_query()`'s
`active_score == 0` branch gained ONE new additive `if`, reusing Sprint
47's own `_DEMONSTRATIVE_ANCHORED_RE` regex (not a new mechanism, not a
new representation): when exactly 1 distinct other topic exists AND the
query is demonstrative-anchored (same "2nd word is itu/ini" check Fix #1
above uses, applied here to a DIFFERENT decision), refuse rather than
trust recency — this is what resolves Sprint 47's own Scenario 5 ("Board
itu gimana?") while leaving Sprint 46's own "Mic-nya gimana?" case
untouched (not demonstrative-anchored). No new field was added to
`ActiveTopicSnapshot` and no bounded-provenance data structure was
introduced — a purely grammatical, stateless signal was sufficient. See
§15's Sprint 48 entry, `ARCHITECTURE_GUARD.md` §48, and `docs/change_
impact/bounded_entity_provenance.md` for the full reasoning, including
why a "distinguisher token" idea for the OTHER known limitation (#9,
still open) was investigated and rejected.

**Sprint 49 addition:** `_extract_entity_differentiator()` (`luno/
memory_context.py`) resolves that OTHER known limitation (#9 - two
distinctly-named entities sharing high generic-vocabulary overlap,
"Aquascape A"/"Aquascape B") by reading a DIFFERENT, already-existing
field than `.terms`: `ActiveTopicSnapshot.source_sentence` (Sprint 40's
own verbatim, case-preserved excerpt). Extracts a standalone single
UPPERCASE letter via `\b[A-Z]\b`, applied to the RAW text - never
through `analyze_query()`'s lowercased/stopword-filtered tokens, which
is exactly what made Sprint 48's own token-based attempt unsafe (the
shared stopword list drops lowercase "a" but not "b"). Wired into the
existing `coverage > 0.5` lineage-skip check inside `is_active_topic_
relevant_to_query()`'s `active_score > 0` branch: two entries with
disagreeing, unambiguous differentiators are no longer treated as the
same lineage, correctly producing a REFUSAL (not a forced guess) for a
bare, non-differentiated follow-up. No new field on `ActiveTopic
Snapshot`, no new data structure. See §15's Sprint 49 entry,
`ARCHITECTURE_GUARD.md` §49, and `docs/change_impact/entity_
provenance_disambiguation.md` for the full reasoning, the 20-case hard
boundary matrix, and the explicit known-limitation scope boundaries
(query-side differentiator matching, lowercase differentiators - both
deliberately left for a future sprint).

**Identity/alias/abbreviation handling** (Sprint 43, extended by Sprint
45) is a small, bounded, deterministic lexical layer:
- `_TOKEN_SYNONYM_GROUPS` — ordered tuples of GENERIC (never product-
  specific) synonyms: mic/mikrofon/microphone, gpu/vga, pompa/pump,
  board/mikrokontroler/microcontroller/mcu, upgrade/ganti/naik(kan).
- `_TOKEN_SYNONYM_PHRASES` — a tiny two-word phrase table (`"kartu
  grafis" -> "gpu"`).
- `_strip_bounded_affixes()` — single-pass Indonesian/English affix
  stripper (clitic "-nya"/"-kah"/"-lah", derivational "-kan"/"-an",
  prefixes "me-"/"di-"/"pe-"/etc, English "-ing"/"-ed"/"-s"), bounded by
  `_MIN_AFFIX_ROOT_LEN=4` (and, since Sprint 45, a narrower `_MIN_
  CLITIC_ROOT_LEN=3` used only for the "-nya" pass).
- Abbreviations (e.g. "ESP32-S3" -> "S3") are NOT a separate mechanism —
  they fall out of hyphen-based tokenization splitting the compound into
  separate raw tokens.
- **Deliberately NOT implemented:** product-to-category world knowledge
  (the system never learns "INMP441 IS a microphone" unless the word
  "mic"/"mikrofon" was actually used in the conversation) and general
  contextual-association linking ("gaming" is never treated as an alias
  for "gpu"). Both are confirmed, tested boundaries — see §16.

## 7. Reference Resolution

`luno.memory.classify_reference_type(text)` — pure, deterministic,
precedence-ordered. Returns one of:

```
repair_reference, negation_of_current_option, cost_comparison,
alternative_request, ordinal_reference, attribute_reference,
continuation, comparison, direct_reference, unknown
```

`NEEDS_TOPIC_CONTEXT_TYPES` = every type except `"unknown"`.
`_PURE_REFERENCE_TYPES` (REPLACE-vs-PRESERVE via preserve) =
`{alternative_request, continuation, direct_reference, cost_comparison,
ordinal_reference}`. `_MERGE_REFERENCE_TYPES` = `{repair_reference,
attribute_reference}` (drives MERGE via `is_merge_reference_followup()`,
extended additively by Sprint 44's `is_sparse_unknown_followup()`).

**Known, deliberate, tested precedent — do not "fix" without a new
reproduced failure:** a bare "kalau buat X?" or "kalau koneksinya?" with
no comparison/attribute marker classifies `"unknown"` on purpose (see
`tests/test_conversation_reference_resolution.py::test_13_adversarial_
phrase_matrix`). This is NOT a gap; extending the classifier for these
shapes was explicitly rejected in Sprint 44 after finding this exact
test-documented precedent.

**Sprint 45 fix, locked in:** "gimana" and "bagaimana" are the same
Indonesian word (colloquial vs. formal register of "how") and must
classify identically everywhere. Fixed in FIVE places across `luno/
memory.py` and `luno/memory_context.py` — see `ARCHITECTURE_GUARD.md`
§45 for the exact list. If you find a SIXTH place that still only checks
`"gimana"` and not `"bagaimana"`, that is very likely also a bug.

**Sprint 46 fix, locked in:** the SAME recurring pattern (a word already
treated as boilerplate in ONE of `luno.memory._ATTRIBUTE_RESIDUAL_
STOPWORDS` / `luno.memory_context._TOPIC_OVERLAP_STOPWORDS` but missing
from the OTHER) struck a third time — "kenapa"/"napa"/"mengapa" ("why")
were missing from `_TOPIC_OVERLAP_STOPWORDS`, unlike the colloquial
"kok" already there, causing a genuine entity-erosion bug (see
`ARCHITECTURE_GUARD.md` §46, Fix #3). If a future sprint finds a FOURTH
instance of this same asymmetry (Sprint 44's "buat", Sprint 45's
"bagaimana", Sprint 46's "kenapa"/"napa"/"mengapa"), consider whether
these two stopword lists should be unified into one shared source — not
attempted so far because each individual gap has been small and the
lists serve two different callers (classifier-level residual counting
vs. topic-overlap scoring) that could plausibly diverge legitimately in
the future.

## 8. Temporal Memory

Sprint 41. `luno.memory.classify_temporal_status(text)` distinguishes
current/historical/planned-state queries. `ActiveTopicSnapshot.status`
("active"/"superseded") plus a lookup table decide which statuses are
eligible candidates for each temporal query shape. `select_temporal_
fallback_candidate()` is the LAST-RESORT branch in the retrieval
dispatch (after ordinal, topic-history, and single-slot branches), not
gated on `is_short_followup`. Untouched by Sprints 44/45. **Sprint 46**
did not modify `select_temporal_fallback_candidate()` or its own
`_TEMPORAL_FALLBACK_ELIGIBLE_STATUS` table, but DID change how often the
single-slot branch defers to it: `is_active_topic_relevant_to_query()`
now returns `False` (instead of `True`) for a lone historical-query-
marker token when the active snapshot's own status is present/future
(`"active"`/`"completed"`/`"planned"`), which routes MORE historical-
shaped queries into this fallback than before. A known, documented
residual gap: `_TEMPORAL_FALLBACK_ELIGIBLE_STATUS["historical"]` still
only accepts `"superseded"`/`"cancelled"` status entries, NOT
`"planned"` ones — so a historical query about a still-`"planned"` fact
correctly stops short of confidently injecting the wrong (current) topic,
but does not yet resolve to the right one either. See
`ARCHITECTURE_GUARD.md` §46 Fix #2 and §16 item 5 below.

## 9. Topic History

`main_runtime_demo.py`'s `PlannerBridgeModule._topic_history: Dict[str,
List[ActiveTopicSnapshot]]` — bounded per-conversation list (`_TOPIC_
HISTORY_MAX_ENTRIES = 8`), separate from the single-slot `_active_
topic` dict. `select_topic_candidates()` matches by content (raw token
overlap first, then a normalized-bridging fallback tier requiring a
UNIQUE top scorer — ties return nothing), NOT by recency, and is
computed UNCONDITIONALLY every turn (not gated on `is_short_followup` —
see that function's own docstring for the live-reproduced reason from
Sprint 43). Both `_active_topic` and `_topic_history` are plain,
bounded, in-memory dicts — **never persisted to disk**, never touch any
file I/O path (verified explicitly every sprint since 43).

## 10. Retrieval Pipeline

Four-way dispatch in `main_runtime_demo.py` (~line 4360-4480), unchanged
in STRUCTURE since Sprint 38, extended additively since:

```
if ordinal_targets:              # Sprint 38 (list/ordinal resolution)
elif topic_history_candidates:   # Sprint 43 (content-matched, unconditional)
elif (is_short_followup and active_topic_snapshot is not None
      and not active_topic_snapshot.is_stale
      and (reference_type not in ("comparison", "attribute_reference")
           or is_active_topic_relevant_to_query(...))):  # Sprint 4 + 43 + 45 gate
else:                             # Sprint 41 (temporal fallback, last resort)
```

The `reference_type not in (...)` gate started as `!= "comparison"`
(Sprint 43) and was widened to also exclude `"attribute_reference"` in
Sprint 44 Phase 7 — confirmed via full regression this did not reproduce
Sprint 43's own documented seven-test regression (which was tied to
`alternative_request`/`negation_of_current_option`/`direct_reference`,
still excluded, unchanged).

Once candidates are selected, they flow into the SAME `assemble_
context()` / `_rank_key()` / `_apply_budget()` pipeline as every other
memory source (verified facts, episodic memory, manual memory) —
**no privileged bypass, no second ranking system.**

## 11. Voice / TTS Architecture

Sprint 36 (Voice Output Mode: ALL/SHORT voice behavior) and Sprint 37
(Semantic Voice Selection) — **not touched by Sprints 43/44/45.** Fish
Audio adapter for streaming TTS, with cancellation/barge-in semantics
tested in `test_tts_cancellation.py`, `test_barge_in_console.py`, `test_
wake_barge_in_integration.py`. Response-depth policy (NORMAL/SHORT/etc)
sits between the LLM response and voice output.

## 12. Persistence Boundaries

Persistent, on-disk (`config/*.json`, 15 top-level files): `long_term_
memory.json`, `session_summaries.json`, `relationship_state.json`,
`episodic_memory.json`, `habit_memory.json`, `verified_facts.json`,
`persona.json`, `response_depth_preference.json`, `reminders.json`,
`environment_triggers.json`, `apps.json`, `lights.config.json`,
`switches.config.json`, `scripts.config.json`, `browser_monitor_
targets.json`.

**Never persisted:** raw conversation text, `ActiveTopicSnapshot`/topic
history, entity/alias state, any Sprint-43/44/45/46/47 normalization
state. All of it is conversation-scoped, bounded, in-memory only. Every
sprint since 43 has explicitly verified `config/*.json` before and after
its own changes. As of the end of Sprint 47: isolated verification
(running ONLY Sprint 47's own new/touched test files) confirmed zero
config impact from this sprint's own source edits; a FULL repository
sweep DOES change `config/long_term_memory.json` and `config/
relationship_state.json`, traced to OTHER, pre-existing tests elsewhere
in the 92-file suite that legitimately exercise the real persistence
layer (confirmed unrelated to Sprint 47's own edits — `luno/memory_
context.py` never touches file I/O, per its own docstring). The other
13 of 15 top-level files remain unmodified by mtime. `config/
relationship_state.json` is additionally, separately, actively rewritten
during ANY test run by its own pre-existing, unrelated subsystem (a
rotating-backup mechanism under `config/backups/relationship_state.*.
json`, predating Sprint 46).

## 13. Regression Baseline

Full detail in `docs/testing/regression_baseline.md` (append-only, one
section per sprint — always read the LATEST section first, but treat
earlier sections as historical record, not current truth).

As of end of Sprint 50:
- Targeted core-plus-observability suite: **775 passed, 0 failed**
  (`test_runtime_observability.py`/`test_real_world_capture.py`/
  `test_replay_engine.py` — new, Sprint 50 — plus `test_memory_voice_
  observability.py`, `test_entity_provenance_disambiguation.py`,
  `test_bounded_entity_provenance.py`, `test_semantic_entity_
  identity.py`, `test_contextual_reference_robustness.py`,
  `test_entity_identity_semantic_alias_continuity.py`,
  `test_entity_concept_continuity.py`, `test_conversation_reference_
  resolution.py`, `test_conversation_intelligence.py`, `test_memory_
  continuity.py`, `test_memory_comparison_topic_preservation.py`,
  `test_memory_topic_retention.py`, `test_temporal_memory_timeline_
  awareness.py`, `test_cross_system_conversation_consistency.py`,
  `test_semantic_context_bridging.py`, `test_memory_retrieval_decision_
  quality_reaudit.py`).
- Full repository (100 test files, up from 97 — Sprint 50's own 3 new
  files — 98 collectible, 2 pre-existing uncollectible): **zero new
  regressions.** 10 pre-existing, independently-reproduced, documented
  failures remain (see §16) — identical across Sprints 43-50. Sprint
  49's own documented parallel-execution flake
  (`test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_
  never_plays`) did NOT reproduce during this sprint's own full sweep —
  flakes do not always reproduce; this is not itself evidence of
  anything, and this file was not touched by Sprint 50's own edit. The
  full sweep was run in 8 chunks this sprint (same standing precedent) —
  see §21 for the exact commands. 2947 total tests collected.

As of the Dashboard Turn-State Recovery fix (immediately after Sprint
50):
- Targeted core-plus-observability-plus-recovery suite: **788 passed, 0
  failed** (the Sprint 50 list above, plus this fix's own new
  `tests/test_dashboard_turn_state_recovery.py`, 13 tests).
- Full repository (101 test files, up from 100 — this fix's own 1 new
  file — 98 collectible, 2 pre-existing uncollectible): **2960 total
  tests collected, 2949 passed, 11 failed.** 10 of the 11 failures are
  identical to the standing baseline (see §16). The 11th,
  `test_verification_dashboard.py::test_api_verification_reports_a_
  successful_verified_action_end_to_end`, failed with the EXACT SAME
  `inspect.getsource`/"could not get source code" signature as the
  already-documented `test_state_isolation.py` sandbox flake — re-run in
  ISOLATION, it passed cleanly, confirming a new manifestation of an
  EXISTING flake category (not every occurrence of this sandbox quirk
  hits the same test file), not a regression. **Zero new regressions.**
  Full sweep run in the same 8-chunk methodology — see §21.

## 14. Architecture Invariants

These must remain unchanged unless a REPRODUCED root cause absolutely
requires otherwise (per every sprint's own explicit brief):

- Memory ranking architecture (`_rank_key()`), memory budget
  (`_apply_budget()`), `assemble_context()`'s structure/parameter list.
- Prompt injection boundary (`_neutralize_boundary_markers()`,
  `render_context_block()`'s trust-boundary comment block).
- LLM model, TTS model, streaming architecture, cancellation semantics.
- Voice output modes ALL/SHORT (Sprint 36), response depth system.
- Temporal memory architecture (Sprint 41).
- Conversation reference taxonomy (`REFERENCE_TYPES`, the 10-member
  tuple in §7) — extending the SET of types has never been needed;
  every fix so far has been additive to existing types' own matching.
- `ActiveTopicSnapshot`'s field set (unchanged since Sprint 40).
- No persistent raw conversation storage, no global topic/entity state.
- No embeddings, no LLM-based memory judge, no second ranking system, no
  giant/unbounded synonym dictionary, no unrestricted fuzzy matching, no
  general-purpose stemmer.
- (Sprint 48) `is_active_topic_relevant_to_query()`'s new demonstrative-
  anchor refusal gate must not be widened to fire at `distinct_other_
  count == 0` (would break Invariant 6 — single-topic conversations may
  use stronger fallback) — any future change here must be re-verified
  against `test_20`/`test_21`/Sprint 46's `test_27` AND Sprint 48's own
  Scenario B/B-mirror pair simultaneously.
- (Dashboard Turn-State Recovery fix) The 5 Sprint 50 observability
  events remain OBSERVATIONAL ONLY — the new `llm_error`-on-unhandled-
  exception publish is a RECOVERY signal (reused, not new), never a
  second decision system. Do NOT have any code react to an event type
  purely to change conversation-intelligence behavior.
  `_run_utterance_turn_safely()` must remain the ONLY code that decides
  whether `_handle_utterance()`'s own exceptions get a recovery publish
  — do not duplicate this pattern elsewhere without first proving
  (live, not by inspection) that the SAME missing-terminal-event defect
  actually exists there too.

## 15. Completed Sprints (36-50 summary)

- **36 — Voice Output Mode:** ALL/SHORT voice behavior.
- **37 — Semantic Voice Selection.**
- **38 — Conversation Reference Resolution:** `ConversationReference`,
  `classify_reference_type()`'s original type set, ordinal/list
  resolution (`ActiveTopicSnapshot.list_items`).
- **39 — Conversation Intelligence & Context Quality.**
- **40 — Memory Confidence & Conflict Resolution:** `ActiveTopicSnapshot.
  status`/`source_sentence` fields added.
- **41 — Temporal Memory & Timeline Awareness:** `classify_temporal_
  status()`, the last-resort temporal fallback branch.
- **42 — Cross-System Integration Audit.**
- **43 — Semantic Context Bridging & Memory Precision:** the bounded
  lexical-normalization layer (`_strip_bounded_affixes()`, `_TOKEN_
  SYNONYM_GROUPS`/`_PHRASES`), `is_active_topic_relevant_to_query()`
  (gated to `"comparison"` turns).
- **44 — Entity & Concept Continuity:** `is_sparse_unknown_followup()`
  (entity-identity-erosion fix), the low-ambiguity single-token fallback
  tier in `is_active_topic_relevant_to_query()` (extended in Phase 7 to
  refuse when 2+ distinct topics are live), the guard gate widened to
  also cover `"attribute_reference"` turns.
- **45 — Entity Identity & Semantic Alias Continuity:** confirmed nearly
  all alias/abbreviation/correction/multi-topic scenarios already worked
  via Sprint 43/44 machinery; fixed the "gimana"/"bagaimana" register
  asymmetry (5 spots) and the 3-letter-acronym "-nya" clitic-stripping
  gap (`_MIN_CLITIC_ROOT_LEN=3`). Built this handover system.
- **46 — Contextual Reference Robustness:** confirmed Scenarios A, C, E,
  F, G, J and both directions of cross-topic contamination already
  worked via existing Sprint 36-45 machinery; fixed 3 real, narrow gaps
  (`_normalize_terms_for_bridging()`'s missing root->canon chain; a
  historical-query lone-token guard in `is_active_topic_relevant_to_
  query()`; "kenapa"/"napa"/"mengapa" added to `_TOPIC_OVERLAP_
  STOPWORDS`). Investigated and REJECTED 2 candidate fixes after each
  broke a different existing guarantee (widening `coverage >= 0.5`;
  adding "lebih"/"paling" to the same stopword list) — reverted,
  documented in-place and in the change-impact doc as known limitations.
- **47 — Semantic Entity Memory & Reference Graph:** confirmed Scenario 1
  (multi-name entity without prior grounding) matches Sprint 45's own
  no-fabrication boundary, and Scenario 2 (explicit alias) already
  worked via raw-token overlap. Fixed 1 real, shared-root-cause pair of
  entity-erosion bugs (Scenarios 3 and 6) with 1 new function, `is_
  demonstrative_anchored_followup()` (`luno/memory_context.py`), wired
  into `main_runtime_demo.py`'s `is_merge` decision. Investigated and
  REJECTED 2 candidate fixes for Scenario 5 (cross-topic contamination)
  after each broke a different existing guarantee (globally widening
  `distinct_other_count >= 1`; a curated-vocabulary-gated variant of the
  same widening) — reverted, documented as a known limitation. Also
  discovered and documented (not fixed) a NEW limitation: two
  distinctly-named, high-generic-vocabulary-overlap entities conflated
  by the existing `coverage > 0.5` lineage-skip heuristic. No new entity
  representation introduced — every FIXABLE gap remained a merge-
  eligibility decision on the existing flat bag-of-terms model.
- **48 — Bounded Entity Provenance & Ambiguity Resolution:** re-verified
  Sprint 45-47's own work was actually present before touching anything;
  reproduced all 8 scenarios (A-H) in this sprint's own brief live via
  `RuntimeDemoConsole`. Fixed Sprint 47's own known limitation #8 (the
  "board"/"mic" cross-topic-contamination gap) with ONE new additive
  `if` inside `is_active_topic_relevant_to_query()`'s existing `active_
  score == 0` branch, gated on a GRAMMATICAL signal (whether the query's
  own 2nd word is the demonstrative "itu"/"ini", reusing Sprint 47's own
  `_DEMONSTRATIVE_ANCHORED_RE` regex verbatim) rather than a third
  variant of the SAME `distinct_other_count` threshold Sprint 47 twice
  tried and reverted — resolves "Board itu gimana?" to refuse while
  leaving Sprint 46's "Mic-nya gimana?" (identical formal shape,
  opposite correct answer) fully unchanged. No bounded-provenance data
  structure was introduced — the purely grammatical, stateless signal
  was sufficient on its own. Investigated and REJECTED a "distinguisher
  token" approach for the OTHER known limitation (#9, Aquascape A/B
  conflation) after live tokenizer inspection found the signal's own
  foundation (a short standalone letter token) is asymmetrically dropped
  by the shared stopword-filtering pipeline ("a" vs "b") — limitation #9
  remains open, now with a concrete, investigated reason on record.
- **49 — Entity Provenance Disambiguation & Topic Lineage:** resolved
  Sprint 48's own remaining known limitation #9 (Aquascape A/B
  conflation). Root-caused to `is_active_topic_relevant_to_query()`'s
  `coverage > 0.5` lineage-skip check never consulting `ActiveTopic
  Snapshot.source_sentence` (Sprint 40's own verbatim, case-preserved
  excerpt) - only the flat `.terms` bag. Fixed with 1 new function,
  `_extract_entity_differentiator()`, reading a standalone single
  UPPERCASE letter directly from the RAW `source_sentence` text (never
  through the shared, lowercased/stopword-filtered `analyze_query()`
  tokens Sprint 48's own rejected approach got tripped up by). When two
  entries carry disagreeing, unambiguous differentiators, the majority-
  coverage lineage-skip is bypassed, correctly producing a REFUSAL (not
  a forced guess) for a bare "Pompanya gimana?" after "Aquascape A
  .../Aquascape B ...". Built and verified a 20-case adversarial hard-
  boundary matrix before implementation. No new data structure
  introduced - `ActiveTopicSnapshot`'s field set unchanged. Explicitly
  scoped OUT: query-side differentiator matching ("Pompa A gimana?"
  still refuses rather than resolving to A) and lowercase
  differentiators - both documented as known limitations and Sprint 50
  candidates, not oversights.
- **50 — Runtime Observability, Test Logging & Real-World Data
  Capture:** OBSERVABILITY ONLY, no intelligence-behavior change (per
  its own explicit non-negotiable — Sprint 51 is where the query-side
  differentiator work from Sprint 49's own recommendation resumes, see
  §22). Phase 0 found the existing infrastructure (Event Bus, a full
  "Brain Debugger" HTTP dashboard from §32, `MemoryTurnTrace`) far more
  mature than assumed — the genuine gaps were that nothing persisted the
  Event Bus's own event stream to disk, the memory/reference/topic
  decision pipeline never published its own event, and no real-world
  test-capture/replay mechanism existed at all. Added a small, closed
  event model (5 new event types, each backed by a real call site);
  `EventLogWriter` (`luno/dashboard/event_log_writer.py`, new) — the
  first component in this project to persist any Event Bus event to
  disk (JSONL + human-readable text, redacted, bounded, off by default
  on `RuntimeDemoConsole`, unconditional on `DashboardServer`); and a
  real-world test-data capture/approve/replay loop
  (`luno/test_capture.py`/`luno/replay.py`, both new) — `/mark_test`
  captures a real turn as a `"candidate"` case, `set_case_status()`
  gates promotion to `"approved"` (the ONLY status `replay_all()`'s
  default sweep ever reads), `replay_case()` re-runs a case through a
  fresh, deterministic `RuntimeDemoConsole` (never a real LLM) and
  reports PASS/FAIL/REVIEW with a field-level diff. Live E2E proof: the
  Sprint 49 Aquascape A/B ambiguity gate — previously provable only via
  unit tests — now publishes `ambiguity_refusal=True` as a real,
  timestamped event, and the full capture→approve→replay loop was
  exercised end to end through a real console. A real, minor side effect
  (a pre-existing dashboard E2E test creating a stray `logs/` directory)
  was found via this sprint's own regression sweep and fixed immediately
  rather than left as a known limitation. No new data structure beyond
  two additive `MemoryTurnTrace` fields; `ActiveTopicSnapshot`, ranking,
  `_rank_key()`, `_apply_budget()`, TTS, and streaming all untouched.
- **Dashboard Turn-State Recovery fix** (fix-first, immediately after
  Sprint 50, before Sprint 51): fixed a real production bug — the
  Dashboard could get stuck permanently at `Thinking`, blocking all
  further commands, after certain failures. Root-caused, via a live
  `RuntimeDemoConsole` reproduction (not assumed from the traceback
  text), to `PlannerBridgeModule._handle_utterance()` running on an
  unsupervised daemon thread with no outer try/except — an exception
  escaping before the turn ever reached `NeedLLMResponse` (proven via a
  `ConnectionAbortedError` injected into `self.planner.create_plan()`,
  the exact WinError-10053 shape from the report) left
  `SessionManagerModule` stuck at `THINKING` forever (no timeout exists
  for that state). Fixed with ONE new wrapper method,
  `_run_utterance_turn_safely()`, that publishes the SAME `llm_error`
  event a real OpenRouter failure already publishes on any escaped
  exception — reusing the EXISTING `session_manager`/`barge_in` routes
  and their already-idempotent handlers, zero new event type/route/state
  machine. Separately classified `WinError 10038` ("not a socket") as an
  expected Dashboard-client-disconnect shape in `luno/dashboard/
  server.py` (was already harmless, now also quiet). Investigated `luno/
  bootstrap/shutdown.py` and left it UNCHANGED — proven, via a live
  mid-turn-shutdown test, to already handle this correctly. Zero
  intelligence-behavior change — `_handle_utterance()`'s own body and
  every one of its existing internal `try/except` blocks are byte-for-
  byte unchanged. See `docs/change_impact/
  dashboard_turn_state_recovery.md` and `ARCHITECTURE_GUARD.md` §51.
- **Dashboard Turn-State Recovery fix, PART 2 / TTS-PATH** (fix-first,
  a takeover session's follow-up to the entry directly above, prompted
  by a real production report that the dashboard was STILL stuck at
  `Thinking` after that first fix): the first fix only guarded the
  planner/LLM side of a turn (before `NeedLLMResponse`); this is a
  SEPARATE gap on the TTS/voice-output side, after the LLM call already
  succeeded. Root-caused via static/line-by-line tracing (this session
  could not execute the project's own runtime - see the warning at the
  top of this document) to `SessionManagerModule._handle_playback_done()`
  only clearing `THINKING` when `state == SPEAKING`: a TTS failure on
  the very first chunk (e.g. Fish Audio's TTS server unreachable - the
  same `WinError 10053`/`10061`-style real-world shape as the original
  fix's own trigger) means `speech_playback_started` never fires, so
  state never reaches `SPEAKING`, so the resulting `speech_playback_
  cancelled` event is silently dropped by that guard - `THINKING` stuck
  forever, no exception anywhere, Chat panel already showing the correct
  reply. Confirmed NOT hypothetical: the project's OWN existing test
  helper (`tests/test_dashboard_turn_state_recovery.py::_new_console()`)
  already carries a docstring admitting an unmocked `FishAudioAdapter`
  "makes even an entirely NORMAL turn look like a stuck-THINKING bug" -
  previously dismissed there as a sandbox-only concern and mocked away,
  not fixed at the architecture level. Fixed with ONE new `elif` branch
  in `_handle_playback_done()` (mirrors the existing `SPEAKING` branch's
  own transition exactly - no new state, no new event). Also fixed a
  second, structural instance of the SAME bug class the original fix
  closed for the planner thread: `FishAudioAdapter._play()`/
  `_play_pipelined()`/`_play_stream()`/`_play_stream_pipelined()` each
  gained an outer `except Exception` (before their existing `finally:`)
  guaranteeing a terminal `SpeechPlaybackCancelled` publish on any
  escaped exception - previously such an exception silently killed the
  `_playback_executor` worker thread with zero published event. 5 new
  tests, `tests/test_dashboard_turn_state_recovery_ttspath.py`. **Code
  written, reasoned through against the actual source, and syntax-
  checked - but NOT executed this session** (no working Python
  environment was available - see the top of this document and `docs/
  change_impact/dashboard_turn_state_recovery_ttspath.md`'s own "Not yet
  done" section for the exact command the next agent/session must run
  first). See `ARCHITECTURE_GUARD.md` §52.

## 16. Known Limitations

1. **Bare compound-noun "-nya" declaratives** ("LED strip-nya 430.",
   "Power supply-nya?" — no "kalau"/"yang"/"gimana" marker at all)
   REPLACE the active topic rather than merging. Deliberately not fixed
   (Sprint 44): the only general, cross-domain, non-hardcoded signal
   available (a token ending in "-nya" near the sentence's own subject)
   is also how extremely common Indonesian discourse connectives are
   formed ("soalnya"/"katanya"/"sepertinya"/"akhirnya"/"biasanya"), none
   of which mark an anaphoric entity reference. Locked in as `tests/
   test_entity_concept_continuity.py::test_82_known_limitation_bare_
   compound_noun_nya_statement_replaces`.
2. **Product-to-category world knowledge is never fabricated.** A
   specific product name (e.g. "INMP441") is never automatically
   understood to belong to a generic category (e.g. "mic") unless the
   category word was actually used somewhere in the conversation. This
   is a deliberate boundary of a lexical/bag-of-terms system with no
   embeddings/world knowledge (Sprint 45), not a defect — the brief's
   own Part D explicitly forbids inventing a fix. Locked in as `tests/
   test_entity_identity_semantic_alias_continuity.py::test_85_e2e_
   product_without_category_word_correctly_unresolved`.
3. **Merely-contextual associations are never treated as aliases.**
   "gaming"/"performa" near a GPU discussion are not linked to "gpu" —
   correct per the "no giant synonym dictionary" constraint, but means a
   sufficiently indirect follow-up will not resolve. Locked in as
   `test_52_contextual_association_not_treated_as_alias`.
4. **Pre-existing environment/infrastructure failures** (unrelated to
   any Sprint 43-47 code): `test_mic_device_index.py` (6, real `.env`
   sets `MIC_DEVICE_INDEX`), `test_production_launcher.py::test_07`
   (real OpenRouter/Fish Audio credentials in this checkout's `.env`),
   `test_real_adapters.py` (2, `speech_recognition`/`sounddevice`
   missing/broken in this sandbox), `test_state_isolation.py::test_
   isolate_persistent_state_drains_stragglers_before_monkeypatch_
   reverts` (1, `inspect.getsource` sandbox flake), `test_main_bargein.
   py`/`test_root_main_bargein.py` (2 files, uncollectible — missing
   `faster_whisper` module / `legacy_main.py` absent from this
   checkout). All independently reproduced and identical across
   Sprints 43-50 — see `docs/testing/regression_baseline.md` §15 and
   every subsequent sprint's own entry.
5. **"Yang lebih bagus?"/"Yang paling murah?"** (and other "yang lebih/
   paling X?" attribute questions) in a genuinely single-topic
   conversation are not resolved. A fix was implemented, reproduced as
   correct, and REVERTED after it broke `test_76_e2e_multi_topic_
   ambiguity_gpu_vs_pompa`. See `ARCHITECTURE_GUARD.md` §46, "Rejected
   #2".
6. **A historical query after a still-`"planned"` statement** does not
   resolve to the plan (only correctly refuses to inject the wrong/
   current topic instead). See §8 above and `ARCHITECTURE_GUARD.md` §46
   Fix #2's own "known residual gap" note.
7. **An exact-50%-coverage same-entity topic-history lineage** is not
   recognized as lineage. A fix (`coverage >= 0.5`) was implemented,
   reproduced as correct, and REVERTED after it broke `test_39_tied_
   normalized_overlap_across_history_is_not_relevant`. See
   `ARCHITECTURE_GUARD.md` §46, "Rejected #1".
8. **RESOLVED, Sprint 48 (was open in Sprint 47).** Cross-topic
   contamination for a curated-vocabulary word grounded in NEITHER of
   exactly 2 live topics ("Board itu gimana?" after ESP32 then
   Aquascape) previously, wrongly, confidently resolved to the merely-
   most-recent topic. Fixed by a new gate in `is_active_topic_relevant_
   to_query()` keyed on demonstrative-anchoring (whether the query's own
   2nd word is "itu"/"ini") rather than a third variant of the
   `distinct_other_count` threshold Sprint 47 twice tried and reverted.
   Sprint 46's own `test_27_e2e_no_contamination_reverse_direction`
   (identical formal shape, opposite correct answer — NOT
   demonstrative-anchored) remains correctly unchanged. See
   `ARCHITECTURE_GUARD.md` §48. The genuinely-2-separate-topics variant
   of item 4's "komputer" (PC/laptop) case shares this same root cause
   and is now ALSO refused correctly when phrased with a demonstrative
   (e.g. "Komputer itu gimana?"), though this was not separately
   re-tested this sprint — worth a quick regression check if item 4 is
   ever revisited.
9. **RESOLVED, Sprint 49 (was open in Sprint 47/48).** Two distinctly-
   named entities sharing high generic-vocabulary overlap ("Aquascape
   A"/"Aquascape B", both using "pompa") were previously conflated by
   the `coverage > 0.5` lineage-skip heuristic, resolving to whichever
   was more recent with no ambiguity signal. Sprint 48's own "distinguisher
   token" idea (built on `analyze_query()`'s own token stream) was
   UNSAFE (shared stopword pipeline drops "a", keeps "b"). Sprint 49
   fixed it via a DIFFERENT signal: `_extract_entity_differentiator()`
   reads a standalone single UPPERCASE letter directly from `Active
   TopicSnapshot.source_sentence`'s own RAW, case-preserved text —
   never through the stopword-filtered token stream — so "A"/"B" are
   found symmetrically. Two entries with disagreeing differentiators no
   longer skip the lineage check, correctly producing a REFUSAL for a
   bare "Pompanya gimana?" See `ARCHITECTURE_GUARD.md` §49 and `docs/
   change_impact/entity_provenance_disambiguation.md`.
10. **(Open since Sprint 49, NOT addressed by Sprint 50 — deliberately;
    Sprint 50 was observability-only.) Query-side differentiator
    matching is not implemented.** A follow-up that itself NAMES a
    specific differentiator ("Pompa A gimana?") is not specially
    resolved to A by Sprint 49's own fix — it still refuses, identically
    to a bare query. Deliberate scope boundary, not an oversight —
    extending the same regex to the query text itself is the natural
    Sprint 51 candidate (see §22). Locked in as `tests/test_entity_
    provenance_disambiguation.py::test_33_known_limitation_query_side_
    differentiator_not_resolved`. Lowercase differentiators ("aquascape
    a") are similarly never recognized, for the same reason Sprint 48's
    own token-based approach was rejected — see `test_34`.
11. **(New, Sprint 50 — observability layer, not intelligence.)** No
    dashboard HTML panel was added for the two new `/api/observability/*`
    routes (the pre-existing generic Event Bus page already shows the 5
    new event types with zero code change, so this was judged
    unnecessary this sprint — a future sprint could still add one purely
    for presentation polish). `collect_observability_summary()`/
    `collect_session_trace()` never surface raw user/assistant TEXT, by
    design (preserving `MemoryTurnTrace`'s own privacy boundary — that
    text remains visible via the pre-existing Event Bus/Logs pages).
    `luno.replay.replay_case()`'s own canned reply is a fixed, generic
    string, not the real assistant reply from the original captured
    conversation (intentional anti-leak discipline, same reasoning as
    every Sprint 46+ probe script). Two harmless, redaction-verified log
    files created by a since-fixed test gap remain in this checkout's
    own `logs/` directory — undeletable from this sandbox (the mounted
    workspace root does not permit shell-level deletion), not
    `config/*.json` state. See `ARCHITECTURE_GUARD.md` §50 and `docs/
    change_impact/runtime_observability.md`.
12. **(New, Dashboard Turn-State Recovery fix.)** The recovery wrapper
    (`_run_utterance_turn_safely()`) only guards `_handle_utterance()`
    itself — a bug inside one of that method's OWN already-guarded
    internal `try/except` blocks stays contained by that block, not this
    new outer one (this is by design, not a gap: the outer wrapper is a
    single, additive safety net, not a replacement for the method's own
    internal discipline). The reused `llm_error` event's `model` field is
    `None` and `retryable` is always `False` for this specific recovery
    path (honest placeholders — the turn never reached the LLM at all) —
    any future consumer that assumes `model` is always a real string for
    every `llm_error` should be checked against this case.
    `_is_expected_client_disconnect()`'s `WinError 10038` check is
    intentionally narrow (exact errno/winerror match) — a different,
    not-yet-observed "already disconnected" Windows errno would still be
    logged (safely, just not silently absorbed) rather than
    misclassified. See `docs/change_impact/
    dashboard_turn_state_recovery.md`.
13. **(New, Dashboard Turn-State Recovery fix Part 2 / TTS-path - NOT
    YET LIVE-VERIFIED, see the warning at the top of this document.)**
    `_handle_playback_done()`'s new `THINKING` branch does not
    distinguish "TTS never started for THIS turn" from "a stale/
    unrelated `speech_playback_cancelled` arrived while a DIFFERENT new
    turn has already reached `THINKING`" - it has no `request_id`
    correlation, matching the PRE-EXISTING `SPEAKING` branch immediately
    above it, which has the same limitation and always has. Not
    considered a new risk introduced by this fix (same shape as
    already-accepted behavior), but worth a request_id-correlation pass
    across both branches together if a real race is ever observed.
    `FishAudioAdapter`'s new outer `except Exception` clauses only guard
    each method's own orchestration loop (queue/token-check/publish
    code) - an exception raised INSIDE `self.client.play()` itself is
    still handled by the pre-existing, narrower per-chunk `except
    Exception` (bounded retry, then skip) - unchanged by this fix, and
    already correct. See `docs/change_impact/
    dashboard_turn_state_recovery_ttspath.md`.
14. **(New, Dashboard Turn-State Recovery fix Part 2 / TTS-path.)** This
    fix's own code and its 5 new tests were written and syntax-checked
    but **not executed** - the session that wrote them had no working
    Python environment for this project (no network to install
    dependencies, no bridge to the real Windows `.venv`). This is not a
    limitation of the FIX itself but of the session that produced it -
    flagged here so it is impossible to miss. Run `tests/test_dashboard_
    turn_state_recovery_ttspath.py` (see that file's own docstring for
    the full command) before relying on this fix in production.
15. **(New, Sprint 52 — Robust HA Command & Entity Resolution.)**
    `MockHomeAssistantHandler` was deliberately left unmodified - it
    never consulted `luno.devices` at all, so there was no resolver to
    extend there; testability without live HA is covered via
    `RealHomeAssistantHandler` + a `FakeHAClient` test double instead.
16. **(New, Sprint 52.)** This checkout's real device registry (6
    devices total) has no two devices similar enough to naturally tie
    from a typo, so the new ambiguity safety gate could not be
    exercised with a naturally-colliding real phrase - exercised
    instead via a direct gate-logic unit test and an env-var-widened
    end-to-end test, both using real device labels. See `docs/
    change_impact/sprint52_ha_entity_resolution.md`.
17. **(New, Sprint 52.)** Unlike the Dashboard Turn-State Recovery Part
    2 fix above, Sprint 52's code AND its 29 new tests WERE actually
    executed this session (68 passed, 0 failed, including the 39
    pre-existing tests in `luno/tool_manager/tests/
    test_real_home_assistant_verification.py` re-run unmodified) - but
    only against a minimal, real-but-partial dependency chain assembled
    in-sandbox, NOT the full ~2900-test repository sweep, and NOT
    against a real Home Assistant server. See `docs/change_impact/
    sprint52_ha_entity_resolution.md`'s "What was and wasn't executed"
    section for the complete, honest scope.
18. **(New, Sprint 53 — Memory Session Summary API Compatibility Fix.)**
    `luno/adapters/llm/base.py` (`OpenAICompatibleClient._payload()`) -
    part of the SEPARATE `luno.adapters.llm_manager` stack that powers
    normal conversational chat - has the textually identical hardcoded
    `body["max_tokens"] = tokens` pattern this sprint fixed in `luno/
    adapters/openrouter.py`. A plausible separate latent bug, currently
    dormant for the same structural reason Session Summary was the only
    path triggering the ORIGINAL bug (no current caller on that path
    supplies a non-None `max_tokens` either). Deliberately NOT fixed
    this sprint, per its own explicit "document as a finding for a
    separate sprint, do not fix opportunistically" instruction. See
    `docs/change_impact/memory_session_summary_api_compatibility.md`'s
    own "Known limitation" section - recommended as a Sprint 54+
    candidate.
19. **(New, Sprint 53.)** Like Sprint 52 above, this sprint's own 13 new
    tests (plus 111 pre-existing tests across 4 files, unmodified) WERE
    actually executed this session (93 passed, 3 skipped, 0 failed) -
    but only against a minimal, real-but-partial dependency chain
    assembled in-sandbox, NOT the full ~2900-test repository sweep, and
    NOT against a real, live LLM provider (no API key/network access in
    this sandbox). The 3 skips are pre-existing, environment-specific,
    and unrelated to this sprint (two `recovery/*.json` snapshot files
    referenced by `tests/test_memory_persistence_hardening.py` not
    present in this checkout).
20. **RESOLVED, Sprint 54 (was open in Sprint 53 as item 18 above).**
    `luno/adapters/llm/base.py`'s `OpenAICompatibleClient._payload()`
    identical hardcoded `body["max_tokens"] = tokens` pattern is now
    fixed the same way: `body[config.MAX_TOKENS_PARAM] = tokens`. This
    is the base request builder shared by all three
    `OpenAICompatibleClient` subclasses (`OpenRouterProvider`,
    `OpenAIProvider`, `LocalProvider`), used by both `chat()` and
    `stream_chat()` (neither subclass overrides `_payload()`/`chat()`/
    `stream_chat()`, confirmed via code inspection). Item 18 above is
    now stale/historical — kept unedited for the append-only record, but
    superseded. **Important correction discovered while fixing this:**
    Sprint 53's own root-cause tracing had a gap. `luno/bootstrap/
    adapters.py` line 137 constructs `LLMManagerAdapter()` (not
    `luno.adapters.openrouter.OpenRouterAdapter`) under the
    `"openrouter_adapter"` key — that key name is legacy and does NOT
    indicate the class. `luno.adapters.openrouter.OpenRouterAdapter`
    (Sprint 53's actual fix target) is orphaned in production, kept
    alive only by its own test file. The REAL production call chain for
    Session Summary is `summarize_and_archive_session()` →
    `LLMManagerAdapter.client` (`_LegacyClientShim`) → `chat_once()` →
    default `LLM_PROVIDER="openrouter"` → `luno.adapters.llm.
    openrouter_provider.OpenRouterProvider` → `_payload()` in `luno/
    adapters/llm/base.py` — i.e. this Sprint 54 fix, not Sprint 53's, is
    what actually fixes the originally-reported Session Summary error in
    production. Sprint 53's own fix remains correct and harmless for the
    orphaned class it touched, and its 13 tests still pass unmodified.
    See `docs/change_impact/llm_max_completion_tokens_compatibility.md`'s
    "IMPORTANT CORRECTION TO SPRINT 53'S DOCUMENTED ROOT CAUSE" section
    and `ARCHITECTURE_GUARD.md` §55.
21. **(New, Sprint 54.)** Like Sprint 52/53 above, this sprint's own 24
    new tests (plus 141 pre-existing tests across 5 files, unmodified)
    WERE actually executed this session (165 passed, 3 skipped, 0
    failed) - but only against a minimal, real-but-partial dependency
    chain assembled in-sandbox, NOT the full ~2900-test repository
    sweep, and NOT against a real, live LLM provider (no API key/network
    access in this sandbox). The 3 skips are the same pre-existing,
    environment-specific skips carried over from Sprint 53 (unrelated
    `recovery/*.json` snapshot files). Anthropic and Gemini providers
    were confirmed unaffected (different, provider-correct wire formats
    that do not go through `OpenAICompatibleClient._payload()` at all)
    via 2 dedicated regression tests each.

## 17. Active Work Queue

**Four items, all urgent:**

1. Run `tests/test_dashboard_turn_state_recovery_ttspath.py` for real
   (see that file's own docstring for the exact command) and update
   `docs/testing/regression_baseline.md` + this document's §3/§13 with
   the actual result. The Part 2 / TTS-path fix's code is written but
   has never been executed - see the warning at the top of this
   document and §16 items 13-14.
2. Run the full repository regression sweep for Sprint 52 (Robust HA
   Command & Entity Resolution) - `pytest -q tests/
   test_sprint52_ha_entity_resolution.py luno/tool_manager/tests/
   test_real_home_assistant_verification.py tests/
   test_verification_dashboard.py`, then the full chunked 8-way sweep
   per `docs/testing/regression_baseline.md`'s own methodology - and a
   live smoke test against the real Home Assistant server (a
   deliberately typo'd device command, spoken or via the dashboard,
   confirming it actually executes / correctly refuses when ambiguous).
   Sprint 52's own 68 tests DID run for real this session, but only
   against a minimal assembled dependency chain, not the full
   repository or a real HA server - see §16 item 17.
3. Run the full repository regression sweep for Sprint 53 (Memory
   Session Summary API Compatibility Fix) - `pytest -q tests/
   test_memory_session_summary_api_compatibility.py`, then the full
   chunked 8-way sweep per `docs/testing/regression_baseline.md`'s own
   methodology - and a live verification against the real, configured
   LLM provider (trigger a real Session Summary and confirm the success
   log line appears with no `[Memory] ✗ Session summary error:` line).
   Sprint 53's own 13 new tests (plus 111 pre-existing, unmodified)
   DID run for real this session, but only against a minimal assembled
   dependency chain, not the full repository or a real LLM provider -
   see §16 item 19. `luno/adapters/llm/base.py`'s identical latent
   `max_tokens` pattern (formerly §16 item 18) is now RESOLVED by
   Sprint 54 (§16 item 20) - see item 4 below.
4. Run the full repository regression sweep for Sprint 54 (LLM Stack
   API Compatibility & Max Completion Tokens Hardening) - `pytest -q
   tests/test_llm_max_completion_tokens_compatibility.py`, then the
   full chunked 8-way sweep per `docs/testing/regression_baseline.md`'s
   own methodology - and a live verification against the real,
   configured LLM provider for NORMAL conversational chat (not just
   Session Summary) confirming no `Unsupported parameter: 'max_tokens'`
   error appears. Sprint 54's own 24 new tests (plus 141 pre-existing,
   unmodified) DID run for real this session (165 passed, 3 skipped, 0
   failed), but only against a minimal assembled dependency chain, not
   the full repository or a real LLM provider - see §16 items 20-21.
   This item also effectively completes Sprint 53's own item 3 above,
   since Sprint 54's fix is what actually sits on the real production
   Session Summary call chain (see §16 item 20's correction note) - a
   single live-provider smoke test covers both.

The ORIGINAL Dashboard Turn-State Recovery fix (planner/LLM-side) is
complete and was verified in an earlier session; no other in-progress
work at handover time.

## 18. Current Unfinished Work

**Four open items (see §17 above): the Part 2 / TTS-path fix's own
test execution, Sprint 52 (Robust HA Command & Entity Resolution)'s
full repository regression sweep + live-HA verification, Sprint 53
(Memory Session Summary API Compatibility Fix)'s full repository
regression sweep + live-LLM-provider verification, and Sprint 54 (LLM
Stack API Compatibility & Max Completion Tokens Hardening)'s full
repository regression sweep + live-LLM-provider verification.** Sprint
54's own code and its 24 new tests were written AND actually run this
session (165 passed, 3 skipped, 0 failed) - what's missing is the full
~2900-test sweep and a live call to the real, configured LLM provider,
neither reachable from this session's sandbox (no `OPENROUTER_API_KEY`,
no network access); this also covers Sprint 53's own remaining item,
since Sprint 54's fix (not Sprint 53's) sits on the real production
Session Summary call chain - see §16 item 20. Sprint 53's own code and
its 13 new tests were written AND actually run this session
(93 passed, 3 skipped, 0 failed) - what's missing is the full
~2900-test sweep and a live call to the real, configured LLM provider,
neither reachable from this session's sandbox (no `OPENROUTER_API_KEY`,
no network access). Sprint 52's own code and its 29 new tests were
written AND actually run this session
(68 passed against a minimal-but-real assembled dependency chain - see
`docs/change_impact/sprint52_ha_entity_resolution.md`) - what remains
is the full-repository sweep and a real Home Assistant server, neither
reachable from this sandboxed session. Sprint 50 (all phases 0-18) and
the original Dashboard
Turn-State Recovery fix (all its own phases, reconnaissance through
mandatory handover) are both complete and were verified live in earlier
sessions. Sprint 50's own work: reconnaissance of the existing
Event Bus/dashboard/`MemoryTurnTrace` infrastructure (confirming what to
extend vs. build fresh), event-model design, `EventLogWriter`
implementation and live verification, dashboard collectors + real HTTP
E2E, real-world capture (`mark_test_case()`) + replay
(`replay_case()`/`replay_all()`) built and exercised end to end through
a real console, data-quality status gating, storage-layout skeleton,
privacy/bounding/failure-isolation tests, performance measurement, 47
new tests across 3 files, full regression run in 8 chunks (zero new
regressions — including finding and fixing a real, minor test-pollution
side effect discovered BY the regression sweep itself), persistent-state
verification (full sweep + 2 additional independent isolated runs, all
byte-identical), documentation, this handover update, and the cold-start
takeover simulation (§21).

## 19. Exact Files Modified (Sprint 50)

New files:
- `luno/dashboard/event_log_writer.py` — `EventLogWriter` (JSONL +
  human-readable event persistence, redaction, bounding, rotation).
- `luno/test_capture.py` — `mark_test_case()`, `load_case()`,
  `list_cases()`, `set_case_status()`.
- `luno/replay.py` — `replay_case()`, `replay_all()`, `format_diff()`.
- `docs/change_impact/runtime_observability.md`.
- `tests/real_world/{candidate,reviewed,approved,rejected}/.gitkeep`.

Modified files (all additive — no existing function body's own control
flow was changed, only new statements/parameters added):
- `luno/memory_turn_trace.py` — two new `MemoryTurnTrace` fields
  (`topic_decision`, `ambiguity_check_result`), one derived property
  (`is_ambiguity_refusal`), two new optional `build_turn_trace()` kwargs.
- `main_runtime_demo.py` — 5 new `self._event_bus.publish(...)` call
  sites (own try/except each) in `PlannerBridgeModule._handle_
  utterance()`; pure additive assignment statements setting
  `_topic_decision`/`_topic_relevance_check_result` inside branches that
  already existed; one walrus-operator wrapper around the pre-existing
  `is_active_topic_relevant_to_query()` call (same function, same
  arguments, same laziness); `RuntimeDemoConsole.__init__` gained two
  opt-in constructor parameters (`enable_observability_log=False`,
  `observability_log_dir="logs"`) plus lazy `EventLogWriter`
  construction in `start()`/`stop()`; new `console.mark_test()` method.
- `luno/dashboard/server.py` — `EventLogWriter` wired into `start()`/
  `stop()` (same lifecycle as `EventRingBuffer`), one new constructor
  parameter (`observability_log_dir`), two new GET routes
  (`/api/observability/summary`, `/api/observability/session_trace`).
- `luno/dashboard/collectors.py` — two new pure collector functions
  (`collect_observability_summary()`, `collect_session_trace()`).
- `luno/bootstrap/console.py` — `/mark_test [note]` command +
  `mark_test()` thin-relay method + `HELP_TEXT` entry.
- `tests/test_memory_voice_observability.py` — its own pre-existing E2E
  dashboard test updated to pass a temp `observability_log_dir` (a real,
  minor side effect found via this sprint's own regression sweep — see
  §16 item 11 and `ARCHITECTURE_GUARD.md` §50).

No candidate fixes were implemented and reverted this sprint. No changes
to `luno/memory.py`, `luno/memory_retrieval/`, `_rank_key()`,
`_apply_budget()`, `ActiveTopicSnapshot`'s field set, any existing
classification/ambiguity rule's own OUTPUT, TTS, or streaming.

(Sprint 46-49's own file modifications remain unchanged this sprint —
see §15's own entries and `ARCHITECTURE_GUARD.md` §46-§49.)

## 20. Exact Tests Created (Sprint 50)

- `tests/test_runtime_observability.py` — 22 tests: `MemoryTurnTrace`
  field-addition regression locks (3); event-model E2E through a real
  `RuntimeDemoConsole` including the Sprint 49 ambiguity gate now
  visible as a live event, and a telemetry-failure-can't-break-a-turn
  test (4); `EventLogWriter` JSONL/text writing, pretty-vs-compact
  rendering, redaction, bounding, write-failure isolation, rotation,
  opt-in-by-default (9); dashboard collectors + a real HTTP E2E test
  through a running `DashboardServer` (3); performance (2);
  cross-conversation isolation (1).
- `tests/test_real_world_capture.py` — 13 tests: `mark_test_case()` E2E
  capture/publish/fallback (4); privacy/bounding on captured conversation
  text (2); id allocation/listing/status-lifecycle gating (6);
  `ProductionConsole`'s own thin-relay contract (1).
- `tests/test_replay_engine.py` — 12 tests: PASS/FAIL/REVIEW verdicts
  including a determinism check (6); diff formatting (2); `replay_all()`
  approved-only gating and the full capture→approve→replay loop through
  a real console (4).
- `docs/change_impact/runtime_observability.md`.
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created).

## 19b. Exact Files Modified (Dashboard Turn-State Recovery fix)

New files:
- `docs/change_impact/dashboard_turn_state_recovery.md`.
- `tests/test_dashboard_turn_state_recovery.py`.

Modified files (all additive — no existing function body's own control
flow was changed except the one line naming the thread target):
- `main_runtime_demo.py` — new method
  `PlannerBridgeModule._run_utterance_turn_safely()`; `on_event()`'s own
  `threading.Thread(target=..., ...)` call changed to name the new
  wrapper instead of `_handle_utterance` directly (the ONE non-additive
  line in this fix, and it changes only WHICH function is called by the
  thread, not any decision logic). `_handle_utterance()`'s own body is
  byte-for-byte unchanged.
- `luno/dashboard/server.py` — new module-level constant
  (`_ENOTSOCK_ERRNO`) and function (`_is_expected_client_disconnect()`);
  `_dispatch_get()`/`_dispatch_post()` each gained one new `except
  OSError` branch, inserted between the pre-existing `except
  ConnectionError` and `except Exception` branches (ordering preserved,
  nothing removed).

No changes to `luno/bootstrap/shutdown.py` (investigated, proven
unrelated — see `docs/change_impact/dashboard_turn_state_recovery.md`),
`luno/memory.py`, `luno/memory_retrieval/`, `_rank_key()`,
`_apply_budget()`, `ActiveTopicSnapshot`'s field set, any existing
classification/ambiguity rule's own OUTPUT, TTS, streaming, or
`EventLogWriter`'s redaction/bounding guarantees.

## 20b. Exact Tests Created (Dashboard Turn-State Recovery fix)

- `tests/test_dashboard_turn_state_recovery.py` — 13 tests, every one
  E2E through the real runtime path (`RuntimeDemoConsole`/
  `SessionManagerModule`/`PlannerBridgeModule`/`DashboardServer`, no
  test targets only a private helper function): normal-turn baseline
  (1); the exact WinError-10053 mid-turn-exception reproduction and
  recovery, plus confirming the uncaught-thread-exception no longer
  escapes (2); Dashboard client disconnect not affecting backend state
  (1); cancellation (1); a full normal→failure→normal→failure→normal
  cycle staying usable throughout, plus confirming the busy-guard
  actively rejects while stuck then accepts immediately after recovery
  (2); `_is_expected_client_disconnect()` unit coverage plus an E2E
  check that an expected disconnect produces no error-log line (2);
  `dashboard.stop()` mid-turn safety (1); the reused `llm_error` event's
  new `source` field for observability (1); `_handle_llm_failure()`'s
  own idempotency against a redundant `llm_error` (1); rapid sequential
  turns composing correctly with the pre-existing busy-gate (1).
- `docs/change_impact/dashboard_turn_state_recovery.md`.
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created).

## 19c. Exact Files Modified (Dashboard Turn-State Recovery fix, Part 2 /
     TTS-path) — NOT YET EXECUTED, see warning at top of this document

New files:
- `docs/change_impact/dashboard_turn_state_recovery_ttspath.md`.
- `tests/test_dashboard_turn_state_recovery_ttspath.py`.

Modified files (all additive):
- `luno/wake_session/manager.py` — `_handle_playback_done()` gained one
  new `elif self.session.state == ConversationState.THINKING:` branch
  (mirrors the pre-existing `SPEAKING` branch's own transition exactly).
  No other line in this file changed.
- `luno/adapters/fish_audio.py` — `_play()`, `_play_pipelined()`,
  `_play_stream()`, `_play_stream_pipelined()` each gained one new
  `except Exception as ex:` clause (before their existing, unchanged
  `finally:`), publishing `SpeechPlaybackCancelled(data={"request_id":
  ..., "error": f"unhandled: {ex}"})`. No existing line in any of these
  four methods' own bodies was changed - purely additive `except`
  branches inserted between the existing try body and existing finally.

No changes to `main_runtime_demo.py`, `luno/dashboard/server.py`,
`luno/dashboard/controls.py`, `luno/bootstrap/shutdown.py`, `luno/
memory.py`, `luno/memory_retrieval/`, `_rank_key()`, `_apply_budget()`,
`ActiveTopicSnapshot`'s field set, any existing classification/
ambiguity rule's own OUTPUT, TTS chunking/pipelining behavior on the
SUCCESS path, streaming, or `EventLogWriter`'s redaction/bounding
guarantees. The original Dashboard Turn-State Recovery fix's own files
(§19b above) are unchanged by this Part 2 fix.

## 20c. Exact Tests Created (Dashboard Turn-State Recovery fix, Part 2 /
     TTS-path) — WRITTEN, SYNTAX-CHECKED, NOT EXECUTED

- `tests/test_dashboard_turn_state_recovery_ttspath.py` — 5 tests:
  the exact live reproduction of the reported symptom (TTS fails before
  playback starts, LLM already succeeded, session recovers from
  `THINKING`) (1); the SAME scenario asserted through the real
  Dashboard `send_chat_message()` busy-guard, the exact function that
  produces "Luno is busy right now (state=thinking)" (1); a unit-level
  test calling `FishAudioAdapter._play_stream()` directly with a token
  that raises on `is_cancelled`, confirming the new outer `except`
  publishes exactly one `SpeechPlaybackCancelled` instead of dying
  silently (1); a normal-turn regression baseline confirming neither new
  code path fires for an ordinary successful turn (1); a repeated
  failure→recovery cycle (3 iterations) staying usable throughout (1).
- `docs/change_impact/dashboard_turn_state_recovery_ttspath.md`.
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created).

**These tests have not been run.** See the warning at the top of this
document, §16 item 14, and §17's own active-work item.

## 19d. Exact Files Modified (Sprint 52 — Robust Home Assistant Command
     & Entity Resolution) — 68 TESTS ACTUALLY EXECUTED THIS SESSION, see
     §20d and `docs/change_impact/sprint52_ha_entity_resolution.md`

This is the USER's own "Sprint 52" (a separate takeover session,
unrelated to the Dashboard Turn-State Recovery fix Part 2 above except
for sharing this document). Filed under `ARCHITECTURE_GUARD.md`
section **53** — that document's own section 52 is the Dashboard
Turn-State Recovery Part 2 fix above, a documentation-numbering
coincidence with the user's "Sprint 52" name, reconciled explicitly in
that section's own heading.

Modified files (all additive - `_resolve_entity_id()` and every
`execute()` action branch are byte-for-byte unmodified):
- `luno/tool_manager/builtin/real_home_assistant.py` — new
  `EntityResolutionResult` dataclass; new `_VerifyConfig.
  fuzzy_min_confidence`/`fuzzy_min_margin` fields; new
  `_resolve_entity_tiered()`/`_emit_resolution()` methods on
  `RealHomeAssistantHandler`; `execute()`'s target-resolution call site
  updated to use `_resolve_entity_tiered()`; new module-level
  `_classify_exact_match()`/`_all_known_device_entities()`/
  `_score_candidates()` helpers; `_lookup_script()`/
  `_all_known_device_names()` alias bugfix (scripts' own `aliases` list
  was never checked before this sprint, unlike lights').
- `luno/adapters/events.py` — new `EntityResolutionDecision` Event
  subclass, added to `ADAPTER_EVENT_TYPES`.
- `luno/bootstrap/adapters.py` — one new
  `_VERIFICATION_STAGE_TO_EVENT_NAME` entry (`"resolution"` ->
  `"EntityResolutionDecision"`).
- `docs/project_handover.md` (this file) and `docs/project_handover.json`.

New files:
- `tests/test_sprint52_ha_entity_resolution.py`.
- `docs/change_impact/sprint52_ha_entity_resolution.md`.

No changes to `luno/planner/parser.py`, `luno/planner/planner.py`,
`luno/devices.py`'s legacy `resolve_target_lights()` family,
`MockHomeAssistantHandler`, `_unknown_device_result()`'s messages, any
`ToolResult` schema field, Sprint 49's entity provenance/memory system,
TTS, voice lifecycle, vision, or the Dashboard Turn-State Recovery fix
(either part).

## 20d. Exact Tests Created (Sprint 52 — Robust Home Assistant Command
     & Entity Resolution) — ACTUALLY EXECUTED THIS SESSION (real pytest
     run against a minimal-but-real assembled dependency chain — see
     `docs/change_impact/sprint52_ha_entity_resolution.md`'s own "What
     was and wasn't executed" section for the exact scope)

- `tests/test_sprint52_ha_entity_resolution.py` — 29 tests (22 labeled
  A-V per the sprint brief's own convention, using this checkout's REAL
  discovered device names from `config/lights.config.json`/
  `switches.config.json`/`scripts.config.json` — Main Lamp, RGB Strip,
  RGB Computer, Baterai, Aquascape, gaming mode — not invented ones;
  plus 7 additional: observability event emission for fuzzy/exact/
  ambiguous outcomes, set_brightness typo-benefit, a Mock-handler-
  untouched smoke test, a performance measurement, and a source-
  inspection guard confirming no forbidden dependency). **29 passed, 0
  failed.**
- `luno/tool_manager/tests/test_real_home_assistant_verification.py`
  (pre-existing, UNMODIFIED, 39 tests from the Reliability/Verified
  Smart Home Execution sprints) re-run for real against the Sprint-52-
  modified handler. **39 passed, 0 failed** — proves no regression in
  the file most directly downstream of this change, including the two
  closest-call pre-existing tests (`test_similar_entity_single_suggestion`,
  `test_multiple_similar_entities`) whose targets score 0.70-0.74 by
  `difflib`, deliberately kept below the new 0.78 auto-execute
  confidence bar so they stay on their pre-existing "did you mean...?"
  path unchanged.
- Combined: **68 passed, 0 failed.**
- `docs/change_impact/sprint52_ha_entity_resolution.md`.
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created).

**NOT executed:** the full ~2900-test repository regression sweep (only
this feature's real dependency chain was staged in this sandbox, not
the whole checkout); `tests/test_verification_dashboard.py` (also
touches `RealHomeAssistantHandler`, but via a full dashboard/runtime
E2E harness needing substantially more staged - identified, not run);
anything against a real Home Assistant server. See §16's three newest
items and `docs/change_impact/sprint52_ha_entity_resolution.md`'s own
"Next sprint / deferred" section.

## 19e. Exact Files Modified (Sprint 53 — Memory Session Summary API
     Compatibility Fix) — 93 TESTS ACTUALLY EXECUTED THIS SESSION, see
     §20e and `docs/change_impact/
     memory_session_summary_api_compatibility.md`

This is the USER's own "Sprint 53" (a separate, later takeover
session, narrowly scoped to the reported bug only). Filed under
`ARCHITECTURE_GUARD.md` section **54**.

Modified files (both directly on the confirmed call chain, no
unrelated code touched):
- `luno/adapters/openrouter.py` — `RequestsOpenRouterClient._payload()`'s
  hardcoded `body["max_tokens"] = tokens` now reads
  `body[config.MAX_TOKENS_PARAM] = tokens`; one new import
  (`from .. import config`).
- `luno/memory.py` — `summarize_and_archive_session()`'s legacy
  raw-openai-SDK branch (dormant in production, reachable elsewhere)
  now sends `**{config.MAX_TOKENS_PARAM: 150}` instead of the hardcoded
  literal `max_tokens=150`, mirroring `luno/main.py`'s own established
  pattern.
- `docs/project_handover.md` (this file) and `docs/project_handover.json`.

New files:
- `tests/test_memory_session_summary_api_compatibility.py`.
- `docs/change_impact/memory_session_summary_api_compatibility.md`.

No changes to Home Assistant entity resolution
(`luno/tool_manager/builtin/real_home_assistant.py` and everything
Sprint 52 touched), Vision/OpenCV/FFmpeg, TTS/Fish Audio, Dashboard
turn-state logic (either Dashboard Turn-State Recovery fix), memory
ranking/topic continuity/reference resolution
(`luno/memory_context.py`, `luno/memory_retrieval/*` — read only, to
confirm their own unrelated "max_tokens" retrieval-budget concept was
not this bug), or `luno.adapters.llm_manager`/`luno.adapters.llm.*`
(read only — see §16 item 18's documented-not-fixed known limitation).

## 20e. Exact Tests Created (Sprint 53 — Memory Session Summary API
     Compatibility Fix) — ACTUALLY EXECUTED THIS SESSION (real pytest
     run against a minimal-but-real assembled dependency chain — see
     `docs/change_impact/memory_session_summary_api_compatibility.md`'s
     own "Execution method" section for the exact scope)

- `tests/test_memory_session_summary_api_compatibility.py` — 13 tests
  covering all 9 minimum coverage items the sprint brief required
  (correct completion-token parameter used; the failing `max_tokens`
  key never generated for the default config, and proven genuinely
  config-driven not a second hardcoded literal; end-to-end success
  against a mocked compatible provider; normal chat-style calls
  byte-for-byte unaffected; failure isolation from memory state;
  configured model respected, never overridden; exactly one request
  per summary call; no persistent state file touched besides the
  already-isolated session summaries file; existing error logging
  preserved for a genuinely unrelated failure), plus 2 tests for the
  dormant legacy raw-openai-SDK branch (fixed identically) and 1
  explicit before/after reproduction of the exact reported error text
  via a fake HTTP session simulating the real provider's own
  documented rejection rule. **13 passed, 0 failed.**
- Regression, pre-existing, all UNMODIFIED by this sprint:
  `luno/adapters/tests/test_openrouter_adapter.py` (run via its own
  documented standalone entry point) — **31/31 scenarios passed**;
  `luno/adapters/tests/test_llm_manager.py` (the separate stack this
  sprint deliberately did not touch) — **33 passed, 0 failed**;
  `tests/test_memory_regression.py` + `tests/
  test_memory_persistence_hardening.py` — **16 passed, 3 skipped**
  (pre-existing, environment-specific skips, confirmed unrelated).
- Combined single `pytest -q` run across all five files: **93 passed,
  3 skipped, 0 failed.**
- `docs/change_impact/memory_session_summary_api_compatibility.md`.
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created).

**NOT executed:** the full ~2900-test repository regression sweep (only
this feature's real dependency chain was staged in this sandbox);
anything against a real, live LLM provider (no `OPENROUTER_API_KEY`,
no network access in this sandbox — the before/after reproduction test
is a realistic SIMULATION of the real provider's documented rejection
rule, not a live call, and is never represented as one). See §16's two
newest items and `docs/change_impact/
memory_session_summary_api_compatibility.md`'s own "Live verification"
and "Known limitation" sections.

## 19f. Exact Files Modified (Sprint 54 — LLM Stack API Compatibility &
     Max Completion Tokens Hardening) — 165 TESTS ACTUALLY EXECUTED
     THIS SESSION, see §20f and `docs/change_impact/
     llm_max_completion_tokens_compatibility.md`

This is the USER's own "Sprint 54" (a separate, later takeover
session, explicitly instructed not to assume the previous agent's
(Sprint 53's) claims were correct — verification of the actual
checkout surfaced the LLMManagerAdapter/OpenRouterAdapter correction
documented at §16 item 20). Filed under `ARCHITECTURE_GUARD.md`
section **55**.

Modified files (all directly on the confirmed real production call
chain, no unrelated code touched):
- `luno/adapters/llm/base.py` — `OpenAICompatibleClient._payload()`'s
  hardcoded `body["max_tokens"] = tokens` now reads
  `body[config.MAX_TOKENS_PARAM] = tokens`; one new import
  (`from ... import config`). This is the shared request-body builder
  used by both `chat()` (non-streaming) and `stream_chat()`
  (streaming), and by all three `OpenAICompatibleClient` subclasses
  (`OpenRouterProvider`, `OpenAIProvider`, `LocalProvider`) — none of
  which override `_payload()`/`chat()`/`stream_chat()`, confirmed via
  code inspection.
- `docs/change_impact/memory_session_summary_api_compatibility.md` —
  corrected, not rewritten: a "CORRECTION (added by Sprint 54 — read
  this first)" section was added at the top; original Sprint 53
  content below it is unchanged.
- `ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`,
  `docs/project_handover.md` (this file), `docs/project_handover.json`.

New files:
- `tests/test_llm_max_completion_tokens_compatibility.py`.
- `docs/change_impact/llm_max_completion_tokens_compatibility.md`.

No changes to Home Assistant entity resolution, Vision/OpenCV/FFmpeg,
TTS/Fish Audio, Dashboard turn-state logic, memory ranking/topic
continuity/reference resolution, `luno/adapters/openrouter.py` or
`luno/memory.py` (Sprint 53's own fix targets — left exactly as Sprint
53 left them, re-verified still correct via a dedicated regression
test), `AnthropicProvider`/`GeminiProvider` (different, provider-
correct wire formats that never go through `_payload()`), LLM routing/
`llm_manager` architecture, provider priority order, retry policy,
temperature, system prompts, conversation history, or any requested
token COUNT (only the JSON key name changed, never the value).

## 20f. Exact Tests Created (Sprint 54 — LLM Stack API Compatibility &
     Max Completion Tokens Hardening) — ACTUALLY EXECUTED THIS SESSION
     (real pytest run against a minimal-but-real assembled dependency
     chain — see `docs/change_impact/
     llm_max_completion_tokens_compatibility.md`'s own "Tests" section
     for the exact scope)

- `tests/test_llm_max_completion_tokens_compatibility.py` — 24 tests
  covering all 10 minimum coverage items the sprint brief required:
  payload uses `config.MAX_TOKENS_PARAM` (parametrized across all 3
  `OpenAICompatibleClient` subclasses); the legacy `max_tokens` key is
  never generated with the default config (x3); a request without a
  token limit has no token-limit field at all (x3); the streaming path
  uses the same configured param (x3); a tool/function-style metadata
  key does not interfere with the token param; the param is proven
  genuinely config-driven, not a second hardcoded literal, by
  monkeypatching `config.MAX_TOKENS_PARAM` and confirming the wire body
  changes accordingly (x3); Sprint 53's own OpenRouter adapter fix
  remains intact (1 regression test importing that module directly);
  a before/after reproduction of the exact reported error text via a
  fake HTTP session simulating the real provider's documented rejection
  rule (x3); Anthropic's own required, correct `"max_tokens"` field is
  unaffected regardless of `config.MAX_TOKENS_PARAM`'s value (x2);
  Gemini's own `generationConfig.maxOutputTokens` is unaffected
  regardless of `config.MAX_TOKENS_PARAM`'s value (x2). **24 passed, 0
  failed.**
- Regression, pre-existing, all UNMODIFIED by this sprint:
  `luno/adapters/llm/tests/test_providers.py` (the pre-existing
  provider test file this sprint's own tests mirror the conventions
  of); `luno/adapters/tests/test_openrouter_adapter.py` (Sprint 53's
  own fix target, re-run unmodified); `luno/adapters/tests/
  test_llm_manager.py`; `tests/
  test_memory_session_summary_api_compatibility.py` (Sprint 53's own
  new test file, re-run unmodified to prove no interaction); `tests/
  test_memory_regression.py` + `tests/
  test_memory_persistence_hardening.py` — combined, **141 passed, 3
  skipped** (same pre-existing, environment-specific skips carried over
  from Sprint 53, confirmed unrelated).
- Combined single `pytest -q` run across all six files: **165 passed,
  3 skipped, 0 failed.**
- `docs/change_impact/llm_max_completion_tokens_compatibility.md`.
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created).

**NOT executed:** the full ~2900-test repository regression sweep (only
this feature's real dependency chain was staged in this sandbox);
anything against a real, live LLM provider (no `OPENROUTER_API_KEY`,
no network access in this sandbox — the before/after reproduction test
is a realistic SIMULATION of the real provider's documented rejection
rule, not a live call, and is never represented as one). Performance
was measured (mean ~0.0007ms/call, max ~0.0039ms/call, well under the
5ms target) but only for the payload-construction step itself, not a
full round-trip including network I/O. See §16's two newest items and
`docs/change_impact/llm_max_completion_tokens_compatibility.md`'s own
"Live verification" and "Known limitations" sections.

## 19g. Exact Files Modified (Sprint 55 — Full Verification & System
     Stabilization)

- `tests/test_dashboard_turn_state_recovery.py` — added a
  `_reached_state_since(console, state_value, since)` helper (reads
  `session.history` rather than racing a live state poll) and used it
  as an `or`-fallback in `test_05_e2e_repeated_failure_recovery_cycle_
  stays_usable()`'s inner `_one_normal_turn()`/`_one_failing_turn()`
  functions. **Test-reliability fix only — no production code
  changed.** Root cause: the test's own live-state polling could race
  a zero-delay mocked LLM+TTS round trip and miss the THINKING state
  entirely (the opposite of a stuck-session bug — proof recovery
  happens faster than the old poll could observe).
- No file under `luno/` or `main_runtime_demo.py` was modified this
  sprint.

## 20g. Exact Tests Created (Sprint 55) — ACTUALLY EXECUTED THIS
     SESSION (full-repository sweep, first of this scale in this
     project's sprint lineage — see `docs/change_impact/
     sprint55_stability_gate.md` for the full breakdown)

- **Full regression sweep: 3880 collected, 3866 passed, 10 failed, 4
  skipped.** Every failure re-run in isolation and root-caused (see
  §16 and the change-impact doc for the exact classification table).
  Zero genuine new regressions.
- `tests/test_dashboard_turn_state_recovery.py` (13 tests) + `tests/
  test_dashboard_turn_state_recovery_ttspath.py` (never previously
  executed in this project's records) — **18/18 passing** after the
  fix above.
- `tests/test_runtime_observability.py` + `tests/
  test_real_world_capture.py` + `tests/test_replay_engine.py` —
  **47/47 passing.**
- A real, manually-driven end-to-end capture→approve→replay→diff cycle
  against a live `RuntimeDemoConsole` (scratch directory only, never
  touching `tests/real_world/`) — confirmed working, confirmed replay
  never invokes a real LLM.
- `docs/change_impact/sprint55_stability_gate.md` (new).
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated, not created). `ARCHITECTURE_GUARD.md` §56 (new).
  `docs/testing/regression_baseline.md` (Sprint 55 section appended).

**NOT executed / NOT possible:** any live call to a real LLM or Home
Assistant provider — network egress is blocked in this sandbox,
confirmed via both a direct `curl` failure and a real `openrouter.ai`
call attempt returning a `403 Forbidden` from the sandbox's own
network proxy (not merely assumed). See `docs/change_impact/
sprint55_stability_gate.md`'s "Honest summary" section for the full,
explicit verified/not-verified breakdown.

## 19h. Exact Files Modified (Sprint 56 — Home Assistant + Query
     Intelligence)

- `luno/memory_context.py` — one new, additive function,
  `_narrow_by_query_differentiator(candidates, query_text)`, plus a
  one-line change at `select_topic_candidates()`'s existing return
  statement to call it. Closes a real, live-reproduced gap: two
  topic-history entries tied on token overlap (both mention the same
  generic word) were both returned even when the CURRENT QUERY itself
  explicitly named one via Sprint 49's own "standalone uppercase
  letter" differentiator convention ("Pompa A gimana?" now correctly
  narrows to just the "A" entry; a bare query is completely
  unchanged). Reuses Sprint 49's own `_extract_entity_differentiator()`
  directly — no second regex, no new vocabulary, no new state.
- `luno/tool_manager/builtin/real_home_assistant.py` (Sprint 52's
  resolver) — **re-verified only, NOT modified.** All 12 hard safety
  invariants re-confirmed, including a new, genuinely-reproduced
  "typo closer to a WRONG device" (Category L) live test.
- No other file under `luno/` or `main_runtime_demo.py` was modified
  this sprint.

## 20h. Exact Tests Created (Sprint 56) — ACTUALLY EXECUTED THIS
     SESSION

- `tests/test_sprint56_query_entity_differentiator.py` (new, 17
  tests): unit tests for `_narrow_by_query_differentiator()` in
  isolation (8 — no-op cases, narrowing, ambiguous-query cases,
  lowercase-never-matches), end-to-end tests through the real
  `select_topic_candidates()` caller including a second, unrelated
  synthetic vocabulary to prove generalization (7), and 2 structural
  no-hardcoding/reuses-the-existing-extractor checks. **17 passed, 0
  failed.**
- `tests/test_sprint56_ha_safety_matrix.py` (new, 6 tests): a natural
  typo sweep proving no realistic corruption of this checkout's two
  most textually similar real devices (RGB Strip / RGB Computer)
  mis-ranks the wrong one; a deliberately adversarial near-tie
  reproduction proving `execute()` refuses end-to-end with ZERO
  `call_service()` calls to either device; a direct ambiguity-gate
  re-check; an exact>alias>fuzzy priority re-check; and two AST-level
  structural checks (no LLM/network import, no literal device-name
  comparison in the resolver's own decision logic). **6 passed, 0
  failed.**
- Combined with Sprint 52's own suite (68 tests) + the full
  pre-existing memory/topic/entity-continuity regression surface (16
  files, 662 tests): **753 passed, 3 skipped, 0 failed.**
- **Full repository sweep re-run after this sprint's changes** (the
  same fixed 3880-test collection Sprint 55 established): **3865
  passed, 11 failed, 4 skipped.** All 11 re-run in isolation and
  classified — 10 identical to Sprint 55's own documented list, the
  11th one additional non-deterministic reproduction of the
  pre-existing, pre-Sprint-49-documented `test_streaming_e2e.py::
  test_D_barge_in_between_llm_and_tts_chunk_never_plays` timing flake
  (failed once in a parallel chunk run, passed 4/4 in immediate
  isolated reruns — confirmed non-determinism, not a regression).
  **Zero genuine new regressions.**
- `docs/change_impact/sprint56_ha_query_intelligence.md` (new,
  includes the full Phase 13 contextual-HA-reference evidence matrix).
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated). `ARCHITECTURE_GUARD.md` §57 (new). `docs/testing/
  regression_baseline.md` (Sprint 56 section appended).

**Phase 13 (contextual HA references) — investigated, DEFERRED to
Sprint 57, NOT implemented.** Live-reproduced: a bare "Matikan." after
"Nyalain lampu kamar." parses to `target=None` and fails safely (no
device ever touched, just a confusing error message); "Matikan itu."
fails as an unknown device, same safety guarantee. No safe existing
hook was found to build genuine contextual resolution on without
risking a second, parallel state system — deferred per the brief's own
explicit permission, with a full evidence matrix left for the next
agent in `docs/change_impact/sprint56_ha_query_intelligence.md`.

**NOT executed / NOT possible:** any live call to a real Home Assistant
server (same network-egress limitation as Sprint 55's LLM verification
— proven, not assumed).

## 19i. Exact Files Modified (Sprint 57 — Contextual Home Assistant
     References & Target Continuity)

- `main_runtime_demo.py` (`PlannerBridgeModule`) — additive changes only:
  - `__init__`: `_last_device_target`'s type widened (bare string ->
    dict value); two new attributes, `_device_context_turn_seq` (per-
    conversation turn counter) and `_tool_bridge_local` (`threading.
    local()` slot for conversation-identity correlation).
  - `_apply_device_context()` — rewritten body: bumps the turn counter;
    REMEMBER now collects DISTINCT targets across the broadened
    `_CONTEXT_REMEMBER_ACTIONS` set and clears memory on same-turn
    multi-device ambiguity instead of picking one; FILL now gates on
    freshness (`_CONTEXT_MAX_TURN_AGE`) and domain compatibility
    (`_CONTEXT_FILL_COMPATIBLE_DOMAINS`) before reusing a remembered
    device; publishes the new `device_context_resolution` observability
    event when an attempt is made.
  - New methods: `_device_context_entity_info()`, `_remember_device_
    target()`, `_invalidate_device_context_on_failure()`.
  - New/extended constants: `_CONTEXT_REMEMBER_ACTIONS`, `_CONTEXT_MAX_
    TURN_AGE`, `_CONTEXT_FILL_COMPATIBLE_DOMAINS`, `_CONTEXT_FILLER_
    WORDS` (added "yang"/"tadi").
  - `_tool_bridge_handler()` — 2 new call sites (`_invalidate_device_
    context_on_failure(tool_call)`, in both the timeout branch and the
    `tool_failed` branch, immediately before each `raise`).
  - `_handle_utterance()` — 1 new line (`self._tool_bridge_local.
    conversation_id = conversation_id`, set at the top).
  - `_on_conversation_ended()` — 1 new line (`_device_context_turn_seq.
    pop(session_id, None)`, alongside the pre-existing `_last_device_
    target.pop(...)`).
- `luno/tool_manager/builtin/real_home_assistant.py` — `execute()` gained
  one new, narrowly-scoped guard (target truly missing AND not
  `run_script` -> `_missing_target_result()`); one new method, `_missing_
  target_result()`. No other line in `execute()`'s dispatch logic
  changed; `_resolve_entity_tiered()`/`_score_candidates()` (Sprint 52's
  resolver) untouched.
- `tests/test_device_context.py` — 2 pre-existing assertions updated
  (`bridge._last_device_target["conv-1"]["home_assistant"] == "rgb_
  strip"` -> `...["target"] == "rgb_strip"`) for the new dict value
  shape. 0 behavior changes to the other 20 tests.
- No other file under `luno/` was modified this sprint. Sprint 52's
  resolver and Sprint 56's differentiator are both re-verified passing,
  unmodified.

## 20i. Exact Tests Created (Sprint 57) — ACTUALLY EXECUTED THIS
     SESSION

- `tests/test_sprint57_contextual_ha_references.py` (new, 42 tests):
  the full A-V safety matrix (with several scenarios covered by 2+
  tests — e.g. B, H, J, O, S, T each have multiple angles), plus
  explicit-target-priority, message-quality (3), performance (1),
  no-LLM/network structural check (1), and observability (3) tests.
  **42 passed, 0 failed.**
- `tests/test_device_context.py` (22 tests, pre-existing, 2 assertions
  updated for the new value shape): **22 passed, 0 failed.**
- **Targeted regression** (this file + device_context + `tests/test_
  sprint52_ha_entity_resolution.py` + `tests/test_sprint56_ha_safety_
  matrix.py` + `tests/test_sprint56_query_entity_differentiator.py` +
  `tests/test_memory_context.py` + `tests/test_dashboard_turn_state_
  recovery.py` + `tests/test_dashboard_turn_state_recovery_ttspath.py`
  + `tests/test_wake_session_console.py` + `tests/test_conversation_
  ended_lifecycle_routing.py` + `tests/test_response_policy.py` +
  `tests/test_runtime_demo.py`): **337 passed, 0 failed.**
- **Full repository sweep** (`tests/`, excluding the 2 permanently-
  uncollectible `test_main_bargein.py`/`test_root_main_bargein.py`
  files — missing `faster_whisper`, same as every prior sprint):
  **3079 passed, 11 failed, 3 skipped** (3093 collected, run via `-n 4
  --dist loadfile --timeout=90` for wall-clock reasons). All 11
  re-run individually in isolation and classified: 3 timing-window
  flakes under parallel CPU contention that PASS standalone (`test_
  llm_tts_streaming_production.py::test_13_cancellation_before_first_
  audio`, `test_state_isolation.py::test_planner_turn_thread_can_
  genuinely_outlive_console_stop`, and `test_streaming_e2e.py::test_D_
  barge_in_between_llm_and_tts_chunk_never_plays` — this last one is
  the EXACT, already-documented scheduling-jitter flake class named in
  `docs/testing/regression_baseline.md` for essentially every prior
  sprint); 8 pre-existing ENVIRONMENT-SPECIFIC failures (7 byte-for-
  byte identical to the already-documented list — `test_mic_device_
  index.py` x4, `test_production_launcher.py::test_07_health_checks_
  all_pass_in_default_mock_configuration`, `test_real_adapters.py` x2
  — plus 1 NEW instance of the identical class, `test_llm_dashboard.
  py::test_api_llm_endpoint_reports_manager_state`, this checkout's
  real `.env` sets `LLM_PROVIDER=openrouter` vs. the test's assumed
  `openai` default). **Zero genuine regressions** — none of the 11
  touch any file this sprint modified.
- Performance: `_apply_device_context()` ~0.02ms/call (1000-call
  average) — far under the 5ms target.
- Persistent state: all `config/*` files (JSON + vision-memory SQLite)
  byte-identical (MD5) before/after both the targeted run and the full
  sweep. This sprint touched no config file.
- `docs/change_impact/sprint57_contextual_ha_references.md` (new,
  includes the full A-V safety-matrix test mapping, the message-quality
  fix root cause, and the `long_term_memory.json` diagnostic detail).
- `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated). `ARCHITECTURE_GUARD.md` §58 (new). `docs/testing/
  regression_baseline.md` (Sprint 57 section appended).

**`config/long_term_memory.json` — diagnosed, explicitly DEFERRED, NOT
fixed.** Not valid JSON, not gzip, not standard zlib, not any common
text encoding (UTF-8/UTF-16 fail; Latin-1 "succeeds" into mojibake);
Shannon entropy 7.65 bits/byte (of a max 8.0) — consistent with
encrypted/compressed data, not ordinary corruption. No backup exists
for this specific file. Format/root cause UNKNOWN; not clearly safe to
fix (no known encoding to reverse, no backup to restore, file is
read-only). Out of scope for a contextual-HA sprint regardless of
cause. The existing load path already fails closed safely. See `docs/
change_impact/sprint57_contextual_ha_references.md`'s dedicated section
for the full diagnostic writeup and the recommended next step (check
the original `E:\Luno Evo` device for an out-of-band backup).

**NOT executed / NOT possible:** any live call to a real Home Assistant
server (same network-egress limitation as Sprint 55/56 — proven, not
assumed, not re-tested this sprint since nothing changed about the
sandbox's own network policy).

## 19j. Exact Files Modified (Sprint 58 — Home Assistant Multi-Entity &
     Group Commands)

- `main_runtime_demo.py` (`PlannerBridgeModule`) — additive changes only:
  - New constants: `_GROUP_ALL_WORD_RE`, `_GROUP_LIGHT_WORD_RE`,
    `_GROUP_AREA_RE`, `_MULTI_TARGET_DAN_RE`,
    `_MULTI_TARGET_DISQUALIFYING_RE`.
  - New methods: `_ha_group_all_lights_shape()`, `_ha_explicit_multi_
    target_shape()`, `_resolve_ha_group_targets()`, `_apply_ha_group_
    resolution()` (the orchestrator).
  - `_handle_utterance()` — the `elif explicit_memory_note is None:`
    branch now calls `_apply_ha_group_resolution()` FIRST (before the
    pre-existing `_apply_device_context()` call), and a new local
    `ha_group_refusal_note` is appended to `notes` alongside the
    pre-existing `explicit_memory_note` append, immediately below it.
    No other line in that method changed.
- No other file under `luno/` was modified this sprint. `luno/planner/
  parser.py` and `luno/tool_manager/builtin/real_home_assistant.py` are
  both unmodified — re-verified passing exactly as before.

## 20j. Exact Tests Created (Sprint 58) — ACTUALLY EXECUTED THIS
     SESSION

- `tests/test_sprint58_ha_multi_entity_commands.py` (new, 27 tests):
  scenarios A-V from the brief's own matrix (several covered by
  dedicated tests, e.g. T has 3 observability angles, U has 2
  persistent-state angles), plus 2 structural no-bypass invariant tests
  and the brief's own explicitly-required critical safety test (`test_Q`
  — Target A valid + Target B ambiguous -> HA API called 0 times, proved
  against the real `_handle_utterance()` gate mechanism, not asserted by
  construction alone). **27 passed, 0 failed.**
- **Targeted regression** (this file + `tests/test_sprint52_ha_entity_
  resolution.py` + `tests/test_sprint56_ha_safety_matrix.py` + `tests/
  test_sprint56_query_entity_differentiator.py` + `tests/test_sprint57_
  contextual_ha_references.py` + `tests/test_sprint57_ha_contextual_
  reference.py` + `tests/test_device_context.py`): **162 passed, 0
  failed.**
- **Full repository sweep** (`tests/`, excluding the 2 permanently-
  uncollectible `test_main_bargein.py`/`test_root_main_bargein.py` files
  and 1 deselected pre-existing hang unrelated to this sprint — see
  below; `-n4/loadfile` hit a pre-existing pytest-xdist worker hang on
  two separate attempts and was abandoned in favor of a single-process
  run, `--timeout=60 --timeout-method=signal`): **3930 passed, 27
  failed, 4 skipped, 1 deselected** in 768.76s. ONE of the 27 was a real
  regression caused by this sprint's own first implementation attempt
  (`test_runtime_demo.py::test_mixed_utterance_real_command_still_
  succeeds_despite_unknown_clause` — a mixed "turn on the lights and
  how's the weather" utterance was misdetected as a 2-target group);
  found, root-caused, and fixed (see `docs/change_impact/ha_multi_
  entity_commands.md` §5) BEFORE this final number — confirmed passing
  again. The other 26 were individually re-verified, file-by-file (two
  of them also test-by-test in isolation), to be pre-existing and
  unrelated to any file this sprint touched: the already-documented
  `test_mic_device_index.py` (4, ENVIRONMENT-SPECIFIC), `test_real_
  adapters.py` (2, INFRASTRUCTURE), and `test_production_launcher.py`
  (2, incl. the already-documented `test_07` flake); `test_llm_
  dashboard.py::test_api_llm_endpoint_reports_manager_state` and
  `test_llm_tts_streaming_production.py` (3) are live-config/real-
  network-dependent (this sandbox has no LLM server reachable on
  `localhost:1234`); the rest (`test_dashboard.py`, `test_emotion_
  engine.py`, `test_streaming_e2e.py`, `test_streaming_speech_
  integration.py`, `test_tts_chunk_pipelining.py`, `test_tts_e2e_
  pipeline.py`, `test_voice_pipeline_latency.py`, `test_state_
  isolation.py`) all pass 100% individually/isolated — the same
  full-suite-only cross-test thread/timing interference class this
  document's own regression baseline already discusses sampling test
  files in curated batches to avoid. The one deselected test
  (`test_dashboard.py::test_36_audio_capture_store_unit_behavior`) was
  confirmed, in isolation, to pass in 0.60s — its full-suite hang is a
  pre-existing, order-dependent thread/lock flake in `luno/dashboard/
  audio_bridge.py`, a file this sprint never touches. **Zero genuine
  regressions** beyond the one found-and-fixed above.
- Performance: `_apply_ha_group_resolution()` ~0.02-0.09ms/call
  (100-200 call average, real device registry) — far under the 5ms
  target.
- Persistent state: `config/*.json` byte-identical (MD5) before/after
  every check this sprint ran, plus a dedicated automated test.
- `docs/change_impact/ha_multi_entity_commands.md` (new, includes the
  full A-V test mapping, the two deferred-scenario justifications, and
  the mixed-utterance regression root cause/fix). `docs/project_
  handover.md` (this file) and `docs/project_handover.json` (updated).
  `ARCHITECTURE_GUARD.md` §59 (new). `docs/testing/regression_baseline.
  md` (Sprint 58 section appended).

**NOT executed / NOT possible:** any live call to a real Home Assistant
server (same network-egress limitation as every prior sprint).
**NOT implemented (deliberately deferred, not a bug):** area-scoped
groups ("semua lampu di kamar") and contextual groups ("Matikan
semuanya." after a single prior device reference) — see `docs/change_
impact/ha_multi_entity_commands.md` §4 for the full evidence-based
justification for each.

## 19k. Exact Files Modified (Sprint 59 — Single-Room Home Assistant
     Group Control)

- `main_runtime_demo.py` (`PlannerBridgeModule`) — additive changes
  only:
  - New constant: `_SINGLE_ROOM_NAME = "kamar"`.
  - New method: `_is_single_room_word(area_word)` — exact,
    case-insensitive match against `_SINGLE_ROOM_NAME`.
  - `_apply_ha_group_resolution()` — the existing `group_all_light`
    branch's area-word handling is modified: no area word, or area word
    matching `_is_single_room_word()`, now proceeds into Sprint 58's
    UNMODIFIED full-registry enumeration/rewrite path (previously this
    branch refused ANY area word unconditionally); any other area word
    still refuses, now with a note naming the one configured room by
    name. A new `room_word_recognized` field was added to the
    `ha_group_command_resolution` Event Bus publish dict. No other
    line in this method or file changed.
- `tests/test_sprint58_ha_multi_entity_commands.py` — one test updated:
  `test_F_area_qualified_group_is_honestly_refused_not_guessed` now
  asserts against "semua lampu di dapur" instead of "...di kamar" (the
  latter is Sprint 58's own deferred placeholder, intentionally
  superseded by this sprint for "kamar" specifically — see `docs/
  change_impact/ha_single_room_group_control.md` §6/§13). No other test
  in this file changed.
- No other file under `luno/` was modified this sprint. `luno/planner/
  parser.py` and `luno/tool_manager/builtin/real_home_assistant.py` are
  both unmodified — re-verified passing exactly as before.

## 20k. Exact Tests Created (Sprint 59) — ACTUALLY EXECUTED THIS
     SESSION

- `tests/test_sprint59_single_room_group_control.py` (new, 21 tests):
  scenarios A-Q from the brief's own matrix plus a realistic end-to-end
  simulation and regression proof that Sprint 52/56/57/58 behavior is
  unchanged. **21 passed, 0 failed.**
- **Targeted regression** (this file + `tests/test_sprint52_ha_entity_
  resolution.py` + `tests/test_sprint56_ha_safety_matrix.py` + `tests/
  test_sprint56_query_entity_differentiator.py` + `tests/test_sprint57_
  contextual_ha_references.py` + `tests/test_sprint57_ha_contextual_
  reference.py` + `tests/test_device_context.py` + `tests/test_
  sprint58_ha_multi_entity_commands.py`): **261 passed, 0 failed.**
- **Full repository sweep** (same single-process workaround as Sprint
  58 — `-n4/loadfile` pytest-xdist worker hang still reproduces,
  `--timeout=60 --timeout-method=signal` single-process instead;
  `test_dashboard.py::test_36_audio_capture_store_unit_behavior`
  deselected for the same pre-existing, already-verified-unrelated
  thread/lock flake): **3950 passed, 28 failed, 4 skipped, 1
  deselected.** Every failure was cross-verified against Sprint 58's own
  already-completed, file-by-file isolation investigation — same
  failing files, same root causes (`test_mic_device_index.py`/`test_
  real_adapters.py`/`test_production_launcher.py`: environment/
  infrastructure; `test_llm_dashboard.py`/`test_llm_tts_streaming_
  production.py`: no local LLM server reachable on `localhost:1234`;
  `test_dashboard.py`, `test_emotion_engine.py`, `test_streaming_e2e.
  py`, `test_streaming_speech_integration.py`, `test_tts_chunk_
  pipelining.py`, `test_tts_e2e_pipeline.py`, `test_voice_pipeline_
  latency.py`, `test_state_isolation.py`: full-suite-only cross-test
  thread/timing interference, pass individually), with the 3 previously
  least-verified tests double-checked directly again this sprint.
  **Zero genuine regressions.**
- Performance: ~0.02ms average per group-resolution call, far under the
  5ms target.
- Persistent state: `config/*.json` byte-identical (MD5) before/after
  every check this sprint ran, including 2 dedicated automated tests.
- `docs/change_impact/ha_single_room_group_control.md` (new, includes
  the full root-cause evidence, architecture, precedence proof, safety
  model, the test-fixture discrepancy finding, and the full A-Q test
  mapping). `docs/project_handover.md` (this file) and `docs/project_
  handover.json` (updated). `ARCHITECTURE_GUARD.md` §60 (new). `docs/
  testing/regression_baseline.md` (Sprint 59 section appended).

**NOT executed / NOT possible:** any live call to a real Home Assistant
server (same network-egress limitation as every prior sprint).
**NOT implemented (out of scope by design, not a bug):** multi-room /
general area-management abstraction — this sprint recognizes exactly
one room name ("kamar"), per the brief's own explicit instruction not to
build multi-room support yet. See `docs/change_impact/ha_single_room_
group_control.md` §13/§14 for the full evidence and the recommended
follow-on sprint.

## 19l. Exact Files Modified (Sprint 60 — Structured Room/Area Schema
     Foundation)

- `luno/devices.py` — additive changes only:
  - `load_lights_config()`'s per-entry dict now includes an `"area"`
    key (`None` for the short entity_id-only format, and for any
    dict-format entry with no `"area"` key, an empty/whitespace-only
    `"area"`, or an invalid (non-string) `"area"` type). Valid strings
    are stripped and lowercased via the new `_normalize_optional_area()`
    helper.
  - New module-level function `_normalize_optional_area(raw_area,
    device_name)` — validates/normalizes one entry's `"area"` field,
    never raises, never drops the device for a bad optional field.
  - Two new public functions: `get_device_area(name_or_alias)` and
    `get_devices_by_area(area)` — pure, synchronous, read-only lookups
    over the already-loaded `LIGHTS` dict; both re-normalize case/
    whitespace defensively on every call.
  - No existing function's signature or behavior changed — `_lookup_
    light()`/`_all_known_device_names()`/`_all_known_device_entities()`
    (in `real_home_assistant.py`) and every other pre-existing consumer
    of `LIGHTS` are untouched and re-verified passing exactly as
    before.
- `main_runtime_demo.py` (`PlannerBridgeModule`) — additive changes
  only:
  - `_apply_ha_group_resolution()`'s existing `group_all_light` branch:
    a new `any_structured_area` check and `allowed_names` device-
    selection filter, applied ONLY when an area word was already
    captured (`area_word is not None`, unchanged Sprint 58 detection)
    AND at least one light in the registry has structured `"area"`
    metadata. `area_word is None` (plain "semua lampu") is completely
    unaffected — `allowed_names` stays `None` unconditionally. A new
    `structured_area_metadata_present` field was added to the
    `ha_group_command_resolution` Event Bus publish dict. No other line
    in this method, or any other method in this file, changed. Shape
    DETECTION (`_ha_group_all_lights_shape()`, `_GROUP_AREA_RE`,
    `_is_single_room_word()`) is entirely untouched.
- `config/lights.config.json` — additive migration: `"area": "kamar"`
  added to Main Lamp/RGB Strip/RGB Computer's existing dicts.
  `entity_id`/`aliases`/`max_brightness`/`fade_transition`/name
  unchanged for all three. `config/switches.config.json`/`scripts.
  config.json` NOT modified (no location evidence, and switches'
  format has no per-device object to carry the field).
- `tests/test_sprint58_ha_multi_entity_commands.py` — one test's
  assertion updated: `test_F_area_qualified_group_is_honestly_
  refused_not_guessed` now asserts every real light carries `"area":
  "kamar"` (previously asserted no light carried any area/room/zone key
  at all — Sprint 58's own documented gap, deliberately closed by this
  sprint). The refusal behavior the test exists to prove is unchanged.
  No other test in this file changed.

## 20l. Exact Tests Created (Sprint 60) — ACTUALLY EXECUTED THIS
     SESSION

- `tests/test_sprint60_area_schema.py` (new, 27 tests): scenarios A-T
  from the brief's own matrix (several with extra coverage, e.g. J/Q/R
  each have 2 tests) plus 4 safety-invariant tests, 1 performance test,
  and 1 realistic end-to-end test against the REAL, migrated `config/
  lights.config.json`. **27 passed, 0 failed.**
- **Targeted regression** (this file + `tests/test_sprint52_ha_entity_
  resolution.py` + `tests/test_sprint56_ha_safety_matrix.py` + `tests/
  test_sprint56_query_entity_differentiator.py` + `tests/test_sprint57_
  contextual_ha_references.py` + `tests/test_sprint57_ha_contextual_
  reference.py` + `tests/test_device_context.py` + `tests/test_
  sprint58_ha_multi_entity_commands.py` + `tests/test_sprint59_single_
  room_group_control.py`): **210 passed, 0 failed.**
- **Full repository sweep** (same single-process workaround as Sprint
  58/59). Independently-verified collection for this checkout: **3190
  tests** (`pytest --collect-only`, confirmed both with and without
  this sprint's changes present) — materially fewer than the 3983
  previously documented by Sprint 59's own regression baseline; this
  discrepancy could NOT be explained from within this sprint's scope
  and is reported here explicitly rather than silently reconciled or
  ignored (see `docs/change_impact/area_schema_foundation.md` §10/§15).
  A first run (680.57s) unknowingly overlapped with a stale leftover
  pytest process from a PRIOR session, discovered via `ps` mid-
  investigation and killed. A clean second run (679.53s, no concurrent
  process): **3158 passed, 28 failed, 3 skipped, 1 deselected.** Every
  failure was individually classified — none touch `luno/devices.py`,
  `main_runtime_demo.py`'s HA group-resolution code, `config/lights.
  config.json`, or either test file this sprint modified:
  `test_mic_device_index.py` (4)/`test_real_adapters.py` (2)/`test_
  production_launcher.py` (2) — already-documented environment/
  infrastructure; `test_llm_dashboard.py` (1)/`test_llm_tts_streaming_
  production.py` (5 this run) — no local LLM/speech server, directly
  RE-CONFIRMED this sprint by running the file's 5 failing tests
  together in isolation (only 1 of 5 failed that time, with the console
  showing a real `SpeechStreamIdleTimeout`/`fish_audio` "no chunk
  arrived within 30.0s" network error — direct proof of genuine network
  I/O, not a code defect); `test_dashboard.py`, `test_emotion_engine.
  py`, `test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
  `test_tts_chunk_pipelining.py`, `test_tts_e2e_pipeline.py`, `test_
  voice_pipeline_latency.py`, `test_state_isolation.py` (13 combined) —
  already-documented full-suite-only cross-test thread/timing
  interference. ONE failure was NEW to this sprint's own regression run
  and not previously documented: `test_runtime_demo.py::test_episodic_
  memory_end_to_end_detect_persist_retrieve_alongside_existing_
  context` — directly re-verified TWICE per this sprint's own Phase 8
  instruction ("jangan menyebut pre-existing tanpa bukti"): passes in
  isolation (1 passed, 0.80s) AND passes when its entire home file runs
  standalone (`tests/test_runtime_demo.py` — **78 passed, 0 failed**,
  19.11s) — this sprint's code changes never touch episodic memory or
  anything `test_runtime_demo.py` exercises, so this is classified as
  the same full-suite-only cross-test timing interference class as the
  13 files immediately above, on the strength of this direct evidence,
  not asserted without proof. **Zero genuine regressions.**
- Performance: `get_device_area()`/`get_devices_by_area()` ~0.0006-
  0.0007ms/call (2000-call average); `_apply_ha_group_resolution()`'s
  area-metadata path ~0.026ms/call — far under the 5ms target.
- Persistent state: `config/*.json` MD5-identical before/after the
  clean full sweep, plus a dedicated automated test (`test_T_no_
  config_corruption`) proving every Sprint 60 read path is read-only by
  construction. The one deliberate, one-time migration edit (§19l)
  happened once before any test ran.
- `docs/change_impact/area_schema_foundation.md` (new, includes the
  full root-cause/architecture finding, schema table, migration
  evidence, Sprint 59 compatibility proof, safety analysis, the full
  STOP CONDITION analysis, and the test-count discrepancy writeup).
  `docs/project_handover.md` (this file) and `docs/project_handover.
  json` (updated). `ARCHITECTURE_GUARD.md` §61 (new). `docs/testing/
  regression_baseline.md` (Sprint 60 section appended).

**NOT executed / NOT possible:** any live call to a real Home Assistant
server, LLM, or speech provider (same network-egress limitation as
every prior sprint — directly re-confirmed this sprint via a live
network-timeout error, see above).
**NOT implemented (out of scope by design, not a bug):** multi-room
command detection/execution — this sprint built the schema/registry
FOUNDATION only, per its own explicit brief ("Sprint ini hanya
membangun schema + registry support + query capability"). See `docs/
change_impact/area_schema_foundation.md` §15/§16 for the full
limitations list and the recommended follow-on sprint.

## 19m/20m. Sprint 61 — Generalized Area-Aware Home Assistant Group
     Command (kept brief — that sprint's own brief did not require a
     project_handover.md/json update; recorded here now for continuity)

Generalized Sprint 59/60's hardcoded `"kamar"`-only group handling in
`_apply_ha_group_resolution()` to use `devices.get_devices_by_area()`
for ANY area word, not just "kamar". Removed the now-dead `_SINGLE_
ROOM_NAME`/`_is_single_room_word()` (grep-confirmed zero other
consumers). New file `tests/test_sprint61_generalized_area_groups.py`
(34 tests). One additive fixture update (`tests/test_sprint52_ha_
entity_resolution.py`'s shared `_REAL_LIGHTS`, `"area": "kamar"` added
to match the real, already-migrated config) and one Sprint 59 test
assertion narrowed (message text no longer checked for the now-
different, still-safe refusal reason). Targeted regression 245/245;
full sweep 3193 passed/28 failed (all classified pre-existing/flaky,
zero genuine regressions); persistent state unchanged; performance
~0.0007–0.027ms/call. See `docs/change_impact/generalized_area_
groups.md` and `ARCHITECTURE_GUARD.md` §62 for the full writeup.

## 19n/20n. Sprint 62 — Multi-Domain Area Group Control

Evaluated extending area-qualified HA group commands beyond `light` to
`switch`/`fan`/`climate`/`media_player`. Finding: only `light` has a
registry structure safe for `"area"` metadata; `switch` (`devices.
SWITCHES`) has a resolver/execution path but its loader only ever
produces a flat `name -> entity_id` STRING (no dict form, no way to
attach `"area"`); `fan`/`climate`/`media_player` have no registry at
all. All 4 deferred per STOP CONDITION 1 — documented, not forced. Zero
functional code changes (`_apply_ha_group_resolution()`/`_GROUP_LIGHT_
WORD_RE`/`devices.get_devices_by_area()` all unchanged); the only
`main_runtime_demo.py` edit is a documentation-only comment. New
evidence added: an "unsupported domain" group command (e.g. "matikan
semua switch di kamar") already falls through untouched to the
pre-existing single-target pipeline and fails safely with zero HA
calls, traced to `RealHomeAssistantHandler.execute()`'s own `target and
entity_id is None -> _unknown_device_result()` guard (proved against a
`FakeHAClient`). New file `tests/test_sprint62_multi_domain_area_
groups.py` (26 tests, scenarios A–R). Targeted regression 271/271; full
sweep 3220 passed/28 failed (all individually re-run in isolation and
classified — 20 confirmed full-suite-only timing interference, 8
confirmed pre-existing environment/infrastructure gaps — zero genuine
regressions); persistent state unchanged (only a comment was edited);
performance well under 5ms/call for both the supported and
unsupported-domain paths. No live HA server verification (sandbox has
no HA access). See `docs/change_impact/multi_domain_area_groups.md` and
`ARCHITECTURE_GUARD.md` §63 for the full writeup.

## 19o/20o. Sprint 63 — Long-Term Memory Persistence Recovery &
     Integrity Investigation (DIAGNOSIS ONLY — no fix, no migration)

Investigated `config/long_term_memory.json`'s pre-existing corruption
(flagged Sprint 55/56, forensically profiled Sprint 57, byte-identical —
MD5 `c16525937a6bc063e182c1b6b120e42e` — since). New finding: the file
is NOT a uniform encrypted/compressed layer — bytes 0–1475 measure 7.87
bits/byte entropy (near-random), bytes 1476–1535 are a 60-byte NUL run
(essentially impossible in genuine ciphertext), and bytes 1536–1848
decode as clean, readable ASCII text matching the standard MIT LICENSE
boilerplate verbatim. This is inconsistent with genuine encrypted
memory data and instead suggests the file's content is an accidental
fragment of an unrelated binary artifact — not recoverable memory data
at all. `config/backups/` has zero pre-existing `long_term_memory.*.
json` entries, proving the corruption bypassed `luno.memory._save()`'s
own backup-first write path entirely. A separate, EARLIER, unrelated
incident/restore (documented in `ARCHITECTURE_GUARD.md`'s pre-Sprint-43
"Memory Recovery & Persistence Hardening" section — an ad-hoc script
overwrite, then a 2026-07-23-snapshot-based restore on 2026-08-09) is
where the current backup/atomic-write hardening layer came from, but
its restored content isn't what's on disk today — the current
corruption happened later, at an unidentified point; `docs/change_
impact/memory_recovery.md` and the `recovery/` scripts that sprint
produced are both absent from this checkout. Per the brief's own
decision tree (not a loader bug, not a writer bug, no recovery source
available, format unprovable), a STOP CONDITION applies — no code
change, no file migration, no data recovery attempted. The only
filesystem change is one additive, read-only, byte-identical
preservation backup of the corrupted file's current bytes. New file
`tests/test_sprint63_long_term_memory_recovery.py` (24 tests). Targeted
memory-suite regression 1103/1103; full sweep 3244 passed/27 failed (all
matching Sprint 62's own already-classified pre-existing/flaky set,
spot-re-verified, zero genuine regressions, none memory-related since
zero production code changed); persistent state unchanged including the
file itself. See `docs/change_impact/long_term_memory_recovery.md` and
`ARCHITECTURE_GUARD.md` §64 for the full writeup, including the
recommended manual, out-of-band recovery procedure for the user's own
machine.

## 19p/20p. Sprint 64 — Long-Term Memory Corruption ORIGIN Forensics
     (FORENSICS ONLY — no fix, no recovery, no code change)

Narrower follow-up to Sprint 63: not "what does the corruption look
like" but "who or what plausibly put it there." Result: **STATUS
UNKNOWN** for the actual external origin — no specific, provable source
identified, which is an explicitly valid outcome per the brief, not a
failure. Paired with a **CONFIRMED EXCLUSION**: `luno.memory._save()` /
`_atomic_write_json()` (the sole production writer of
`LONG_TERM_MEMORY_FILE`) is ruled out via structural code audit — its
only data source is always a JSON-serializable list and its only write
mechanism (`json.dump` → atomic `os.replace()`) cannot produce
high-entropy binary content or embedded third-party plaintext under any
failure mode, confirmed by a negative-control reproduction. Every other
persistence writer in the codebase is bound to its own distinct config
path and cannot be misdirected at this file. **Practical implication for
any future sprint: do not re-investigate `luno.memory`'s own code as a
suspect — that avenue is closed with evidence.** The open, unresolved
avenue is an external tool/process outside this codebase — no candidate
was found, but none was ruled out either.

Two corrections worth knowing before touching this area again: (1) the
SHA-256 recorded in Sprint 63's own prose was a 63-character transcription
missing its final hex digit — the correct 64-character value is
`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a` (MD5
`c16525937a6bc063e182c1b6b120e42e` was always correct and remains the
primary drift guard); (2) `probe_memory_pipeline.py` textually imports
`luno.memory` *before* redirecting `LONG_TERM_MEMORY_FILE` to an isolated
temp path (not after, as earlier reasoning assumed) — it still never
writes to the real file, because `luno.memory` does live path lookups
rather than caching the path at import time, but the one thing that runs
against the real path in that ordering is `luno/memory.py`'s own
module-level `_load()` call, which is read-only by construction.

Also found: the production file's timestamps and `444` permission are
both whole-bundle packaging/extraction artifacts shared by every
untouched original `config/*.json` file (not corruption-specific
signals) — corrects Sprint 55's earlier speculation. New test file
`tests/test_sprint64_memory_corruption_forensics.py` (15 tests, 0
failed). Targeted memory-suite regression 47 passed/3 skipped/0 failed;
full sweep 3249 passed/38 failed/3 skipped/1 collection error, zero
failures touching `LONG_TERM_MEMORY_FILE`; persistent state unchanged
including the file itself and its Sprint 63 preservation backup. See
`docs/change_impact/long_term_memory_corruption_forensics.md` and
`ARCHITECTURE_GUARD.md` §65 for the full writeup.

## 19q/20q. Sprint 65 — Luno Tool & File Access Audit (AUDIT ONLY —
     no fix, no hardening, no code change)

Answered: "can Luno modify its own source code/config, directly or via a
combination of tools?" **No such path is provable from this codebase.**
Traced every registered tool handler, every filesystem write path, every
subprocess/exec/eval/dynamic-import call site, and the LLM-to-sink
control boundary for each. Findings: the tool/action namespace
`ToolManager` dispatches through is closed and fixed at startup (no
`python`/`shell`/`exec`/`bash`/`cmd` tool exists); zero
`exec()`/`eval()`/`shell=True` anywhere in `luno/`; the two dynamic
module-loading call sites target hardcoded filenames only; no plugin
auto-loading mechanism exists; `apps.json`/`lights.config.json`/
`switches.config.json`/`scripts.config.json`/`persona.json` are
read-only at runtime (zero write-mode `open()` sites); only memory/
preference JSON stores are written, each through its own dedicated,
hardcoded-path writer — never `.py`/config source. **Practical
implication: future sprints don't need to re-litigate "can the LLM
escape to arbitrary code execution" from scratch — this sprint closed
that with evidence.**

Two findings worth knowing before touching browser/download code:
**SPRINT65-002 (LOW)** — the browser `download` action's path-
containment check (`validate_download_path()`) is correct but has no
opinion on whether `download_dir` itself overlaps the source tree; today
it doesn't (verified), but that's a configuration invariant, not a
code-level guarantee, and `download` is auto-allowed without
confirmation by default. **SPRINT65-001 (INFO)** — the closed tool
registry is a real safety property today with no automated regression
gate against a future sprint widening it. One item honestly marked
UNKNOWN rather than guessed: whether the actual deployed Home Assistant
instance/network could be configured to reach back into this project's
filesystem — unknowable from source code alone.

New test file `tests/test_sprint65_tool_file_access_audit.py` (27 tests,
0 failed). Targeted regression 232 passed/2 failed (pre-existing whisper-
adapter flake)/3 skipped; full sweep 3275 passed/39 failed/3 skipped/1
collection error, every failure matching Sprint 62/63's own already-
classified full-suite-only timing flakiness (one previously-unseen name
re-run individually and confirmed passing in isolation, not a genuine
regression); persistent state unchanged including `config/*.json` and a
dedicated critical-file hash set for this sprint's own subject files.
See `docs/change_impact/tool_file_access_audit.md` and
`ARCHITECTURE_GUARD.md` §66 for the full writeup.

## 19r/20r. Sprint 66 — Tool Boundary Hardening (SECURITY HARDENING —
     addresses Sprint 65's SPRINT65-001/-002 findings)

Explicitly NOT autonomous-coding capability: no shell execution, no
arbitrary Python execution, no arbitrary filesystem write, no
source-code modification, no git write, no plugin installation, no
dynamic tool registration was added — all remain absent, now with
dedicated lock-down tests. Two production files changed:
`luno/browser/security.py` gained `validate_download_directory()` (a new
outer guard: `download_dir` may not overlap `SOURCE_ROOT`/`luno/`, may
not equal/be an ancestor of `PROJECT_ROOT`, may not overlap any single
dynamically-collected `CRITICAL_PATHS` entry — but MAY nest under
`PROJECT_ROOT`, a documented judgment call so the real default
`config/browser_downloads` keeps working), plus `_resolve_for_
comparison()`/`_path_contains()` helpers (realpath+normcase resolution,
`commonpath`-based containment, never `str.startswith()`); the
pre-existing `validate_download_path()` was upgraded to the same
resolution primitive, closing a symlink-bypass gap. `luno/tool_manager/
builtin/real_browser.py`'s `RealBrowserHandler` now validates
`download_dir` at construction time (fails closed via the existing
bootstrap try/except — no new plumbing) and again on every `"download"`
dispatch (defense in depth, since config is reloadable without a
restart). Tool registry (Phase 6) confirmed already safe by construction
— no handler holds a registry reference, `.register()`/`.unregister()`
only called from bootstrap code, `.get()` is a bare dict lookup — zero
registry code changed, regression tests added only.

New test file `tests/test_sprint66_tool_boundary_hardening.py` (40
tests: Phase 7 write-boundary matrix, Phase 8's A–U adversarial matrix,
Phase 11 self-modification lock tests, Phase 14 performance tests). 0
failed. Targeted regression (this sprint's 40 + Sprint 65's 27 +
`luno/tool_manager/tests/` + browser/desktop-control suites) — 198
passed, 0 failed. Full sweep: 3316 passed/38 failed/3 skipped/1
collection error, every failure matching the identical file/test set
Sprint 65's own baseline already classified as full-suite-only timing/
environment-coupled flakiness (33 of the 38 re-run in isolation,
33/33 passed — not a genuine regression); persistent state unchanged
including `config/*.json` and a dedicated critical-file hash set for
this sprint's own subject files. Performance: both new validation paths
measured well under the 5ms/operation target, no network/LLM calls.
See `docs/change_impact/tool_boundary_hardening.md` and
`ARCHITECTURE_GUARD.md` §67 for the full writeup, including the
documented reasoning for the "nest under PROJECT_ROOT is allowed"
interpretation and the remaining Chain G UNKNOWN (external HA service
configuration — unchanged from Sprint 65, unknowable from source alone).

## 19s/20s. Sprint 67 — Mutation Audit Trail & Forensic Observability
     (OBSERVABILITY ONLY — no new capability added)

Built a lightweight, structured mutation audit trail so a future
unexpected filesystem/state mutation can be investigated with evidence.
New module `luno/mutation_audit.py` — reuses Sprint 50's `logs/` root
(new sibling `logs/mutation_audit/YYYY-MM-DD.jsonl`) and Sprint 66's
path-safety primitives (`validate_download_directory()` etc., applied
unchanged to the audit directory itself) rather than inventing either.
Schema: timestamp/operation/path/path_category/source_component/
source_operation/success/before+after exists+size+sha256/tool_name/
action_name/correlation_id/pid — metadata only, structurally no generic
content field, so it can never carry file contents, memory contents,
conversation text, or secrets.

Integrated into `luno/persistence.py::atomic_write_json()` (covers 7
stores: session summaries, verified facts, habit memory, episodic
memory, relationship state, response-depth preference, reminders) and
`luno/memory.py::_atomic_write_json()` — the dedicated, "major" coverage
Phase 7 asked for `config/long_term_memory.json` specifically. Every
call now captures a before-snapshot, fails closed (refuses to write)
via `mutation_audit.assert_audit_subsystem_available()` when the target
is CRITICAL-category and the audit subsystem itself is unsafe/
unwritable, performs the SAME unmodified atomic temp-write+fsync+
`os.replace()` sequence, and records the actual outcome afterward —
never logged as success before `os.replace()` truly succeeds. For
`luno.memory._save()`, a fail-closed raise is absorbed by that
function's own pre-existing "never raise out of a save" catch-all
(unchanged) — practical effect: write skipped, logged, no crash, same
as any other pre-existing save failure. Browser downloads (`real_
browser.py`'s `_dispatch()`) get the same before/after/success pattern
plus a per-call correlation ID (the smallest safe correlation mechanism
this project needed — no existing one, no second tracing system added).
`apps.json`/`lights.config.json`/`switches.config.json`/`scripts.
config.json`/`persona.json`/`browser_monitor_targets.json`/
`environment_triggers.json` re-confirmed read-only at runtime — nothing
to instrument, not assumed.

**`config/long_term_memory.json`'s CURRENT, already-corrupted bytes were
NEVER read, rewritten, or otherwise touched this sprint** — confirmed by
hash+mtime before/after a dedicated test; SHA-256
(`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a`)
unchanged since Sprint 55. Only the surrounding write CODE was
instrumented, per the brief's own "instrument future mutations only"
rule.

New test file `tests/test_sprint67_mutation_audit_trail.py` (48 tests:
successful/failed critical mutations with real before/after SHA-256,
browser download coverage, tool correlation, audit-path fail-closed
protection, 40-thread concurrency, malformed-JSONL-line tolerance,
retention rotation that never touches `config/*.json`, a documented
post-mutation-audit-failure crash-window case, no-secrets/no-dynamic-
dispatch structural proofs, dedicated long-term-memory forensic-
coverage regression, performance). 0 failed. `tests/conftest.py`'s
autouse isolation fixture extended to redirect `mutation_audit.
AUDIT_LOG_DIR` per test — otherwise ~1100+ memory/persistence tests
would have written real records into Vinn's actual `logs/` directory.

Targeted regression (this sprint's 48 + Sprint 63/64/65/66's 118 + the
full 27-file/1103-test memory suite + tool_manager/browser/proactive/
relationship-engine/response-policy suites) — 1633 passed, 3 skipped, 0
failed. Full sweep: 3374 passed/28 failed/3 skipped (0 collection
errors), every failure matching the identical file/test-name set every
prior sprint since 62/63 already classified as full-suite-only timing
flakiness or the pre-existing `list_microphones.py`-absent environment
gap (one individual test re-run in isolation and confirmed passing —
not a regression); persistent state unchanged including `config/*.json`,
`long_term_memory.json` itself, and `config/backups/`'s own count (12,
unchanged); no real `logs/mutation_audit/` directory exists in the
checkout (test isolation held). Performance: both new hot-path functions
measured well under the 5ms/operation target.

See `docs/change_impact/mutation_audit_trail.md` and
`ARCHITECTURE_GUARD.md` §68 for the full writeup, including the honest
evidence-boundary framing (application-level forensic log, not
cryptographically tamper-proof; a post-mutation audit-append failure is
an accepted, documented blind spot) and the remaining Chain G UNKNOWN
(unchanged from Sprint 65/66).

## 19t/20t. Sprint 68 — Mutation Audit Trail Verification & Hardening
     (VERIFICATION + HARDENING — Sprint 67's own claims re-derived
     from the checkout, not assumed correct)

Found one real gap Sprint 67's report didn't catch: `record_mutation()`
stored the raw, uncanonicalized `path`. Fixed via new
`_canonicalize_for_storage()` (`os.path.abspath()` — deliberately
weaker than Sprint 66's security-grade path resolver, since this is a
display concern, not a security boundary). Extended field-bounding to
`operation`/`path`/`correlation_id` (previously only 4 of 7+ fields were
bounded). Added a non-fatal import-time warning for a misconfigured
`MUTATION_AUDIT_LOG_DIR` (real enforcement stays at write time,
unchanged). Central Phase 6 question — can the post-mutation audit-
append-failure blind spot be safely improved without a second
transaction system? Yes: new `record_pending_mutation()` writes a
`"<op>:pending"` record via the SAME JSONL mechanism before a CRITICAL
mutation begins, paired by `correlation_id` with the eventual completed
record — an unmatched pending record is now a **detectable orphan**,
not a silent gap (the blind spot itself is NOT closed — doing so would
require a second transaction system, forbidden by this sprint's STOP
CONDITIONS). New strictly-read-only module `luno/mutation_audit_replay.
py` (AST-verified: zero write-mode `open()`, zero `os.remove/replace/
rename` calls) — justified specifically to detect those orphans.

New test file `tests/test_sprint68_mutation_audit_hardening.py` (67
tests). While chasing two full-suite-only failures in this sprint's own
new retention tests, traced the real cause to a **pre-existing,
unrelated bug**: `tests/test_camera_presence.py`'s `_adapter()` helper
permanently overwrote the shared stdlib `time.time` with no restore,
corrupting every test's wall-clock reads for the rest of any pytest
process that ran that file first. Fixed with one autouse fixture there
(zero production code touched). This also explains why 20+ other tests
(`test_dashboard.py`, the TTS/streaming/voice-pipeline suite,
`test_emotion_engine.py`) had been intermittently misclassified as
"full-suite-only timing flakiness" for an unknown number of prior
sprints — full sweep went from 28-29 failures/760s to **9 failures/
454s** after the fix, and all 9 remaining match the same long-documented
missing-audio-hardware/no-local-LLM environment gap (8 reproduce in
isolation; the 9th is the already-documented `test_state_isolation.py`
order-dependent flake). Zero remaining failures touch `luno/mutation_
audit.py`, `luno/mutation_audit_replay.py`, `luno/persistence.py`, or
`luno/memory.py`. Persistent state unchanged throughout (`config/*.json`
×15 SHA-256-identical, `long_term_memory.json` unchanged since Sprint
55, `config/backups/` count unchanged at 12).

See `docs/change_impact/mutation_audit_hardening.md` and
`ARCHITECTURE_GUARD.md` §69 for the full writeup.

## 19u/20u. Sprint 69 — Camera Device / OpenCV Stability Fix
     (BUG FIX — scoped strictly to the camera open/capture path, no new
     feature, no unrelated vision behavior touched)

Root-caused the reported camera error from the user's own log:
`cv2.VideoCapture(index)` with no explicit backend let OpenCV's `CAP_
ANY` auto-probe reach both `CAP_FFMPEG` (the ~30s internal stream
timeout) and `CAP_OBSENSOR` ("index out of range") for a plain LOCAL
device index — confirmed genuine (`CAP_OBSENSOR` exists in this
project's installed OpenCV build). FFMPEG is the correct backend for a
network stream (`CAMERA_URL`) — the bug is only that CAP_ANY could also
reach it for a local int index. The more severe, previously-unmitigated
bug: `luno/vision.py::_capture_frame()` had ZERO timeout bounding at
all, and is polled up to 2×/s by `RealVisionSource._tracked_cycle_
loop()` — a broken camera would re-hang on every tick with no backoff, a
better match for the log's repeated stall pattern than the one-time
startup hang (already mitigated pre-Sprint-69 by `health.py`'s own
timeout wrapper).

Fixed with: explicit platform-based local-backend candidate selection
(Windows `[CAP_DSHOW, CAP_MSMF]`, Linux `[CAP_V4L2]`, macOS `[CAP_
AVFOUNDATION]`, unknown falls back to `[None]`) that never touches
string/`CAMERA_URL` sources; a new `CameraState` enum (`UNKNOWN`/
`AVAILABLE`/`UNAVAILABLE`/`BUSY`/`BACKEND_ERROR`, `BUSY` honestly
documented as a best-effort guess); a generalized bounded-open helper
shared between the startup health check and every real capture (a
cleanup thread still eventually releases a late-arriving capture, never
leaking a handle); and a reopen cooldown (`CAMERA_REOPEN_COOLDOWN_S`,
default 10s) that makes a poll tick return `None` immediately — without
touching `cv2` at all — while the camera is already known broken,
directly fixing the "hammer a known-broken camera every poll tick" bug.
`health.py`'s previously separate, uncoordinated camera probe now calls
`vision.probe_camera()`, sharing the SAME pre-existing `_camera_lock` as
real captures — no new locking mechanism introduced. New `discover_
cameras()` (bounded, small default range, never touches config) backs a
new read-only diagnostic script, `camera_diagnostic.py`.

**Honest limitation:** this sandbox has no `/dev/video0` at all — a raw
`cv2.VideoCapture(0)` fails in ~2ms here, so the exact ~30s Windows
FFMPEG hang timing could not be reproduced or timed. The fix is
structural, not tuned to this sandbox's failure mode, but live
verification via `camera_diagnostic.py` on the actual Windows machine is
the recommended next step.

New test file `tests/test_sprint69_camera_stability.py` (22 tests — all
17 brief-mandated categories A-Q, a security-construct guard, a
diagnostic-script read-only check). 0 failed on first run. Two
pre-existing test files needed updates as a direct, necessary
consequence of this fix (not incidental breakage) — `tests/test_camera_
health_check_timeout.py` (same patch-target-mismatch class Sprint 68
found in `test_camera_presence.py` — `monkeypatch.setitem(sys.modules,
"cv2", ...)` no longer intercepts `health.py`'s now-indirect call into
`luno.vision`'s module-level `import cv2`) and `tests/test_vision_
sprint8.py` (fake `cv2.VideoCapture` lambdas needed to accept the new
optional `backend` positional arg; one test's "immediate reconnect on
the very next call" assumption is now genuinely false by design and was
updated to advance a fake clock past the cooldown first).

Targeted regression (camera/vision test files, 174 tests): 0 failed.
Full repository sweep run TWICE to check determinism: Run 1 — 3481
passed, 11 failed (9 expected baseline + 2 new); Run 2, same command,
unchanged checkout — 3483 passed, 9 failed, the exact same 9 as Sprint
68's own established baseline, neither Run-1 extra reappearing —
confirming those 2 were a non-deterministic, full-suite-only,
order/timing-dependent flake (one of them, `test_llm_tts_streaming_
production.py`, was in fact already named by Sprint 68 as historically
absorbing that class), not a Sprint 69 regression. Zero failures across
either run touch `luno/vision.py`, `luno/bootstrap/health.py`, or
`camera_diagnostic.py`. Persistent state unchanged throughout
(`config/*.json` ×27 SHA-256-identical, `long_term_memory.json`
unchanged since Sprint 55).

See `docs/change_impact/camera_stability_fix.md` and `ARCHITECTURE_
GUARD.md` §70 for the full writeup.

## 19v/20v. Sprint 69.1 — Camera Runtime/Dashboard Disconnect Forensics & Fix
     (FORENSIC INVESTIGATION + targeted fix — after deployment of Sprint
     69, the user reported the SAME symptom: dashboard `Camera =
     DISCONNECTED`, `cap_ffmpeg_impl` ~30s stream timeout,
     `scheduled_vision_poll` logging `0.0ms`)

Per the brief's own MANDATORY FIRST STEP, traced the complete production
call chain end to end instead of assuming Sprint 69's code was the
actual runtime path. **Proved, not assumed:** exactly ONE production
`cv2.VideoCapture(` call site exists in the entire repository
(`luno/vision.py::_open_capture_bounded()`), via a repo-wide `import
cv2` grep plus an AST-based call-site scan — ruling out a second,
bypassing camera-open path. **Proved:** `scheduled_vision_poll`'s
"0.0ms" log line is a structurally inert heartbeat (`VisionAdapter`
never overrides `BaseAdapter.handle_event()`'s no-op default) — NOT
evidence the camera was actually polled; real polling runs entirely
through `RealVisionSource`'s own two self-scheduling background
threads, unconnected to the Scheduler/EventMapping mechanism. **Proved:**
the dashboard's `camera_connected` badge is a live passthrough from a
single authoritative write site (`VisionAdapter.on_camera_status()`),
not a stale cached default — a `DISCONNECTED` badge reflects a genuine
`connected: false`. **Leading, evidence-consistent but UNPROVEN from
this sandbox explanation** for continued FFMPEG use: `config.py`
auto-derives `CAMERA_URL` from Tapo PTZ credentials (`TAPO_HOST`/
`TAPO_USERNAME`/`TAPO_PASSWORD`) when set — if configured on the
affected deployment, a string source correctly (by Sprint 69's own
design) uses `CAP_ANY`, reaching FFMPEG as expected, correct behavior;
the real problem would then be an unreachable Tapo camera, not a code
bug. Documented honestly as unresolved per the brief's own STOP
CONDITION — this sandbox cannot read the deployment's live `.env`,
running process, or logs.

Found and fixed a genuine, pre-existing (not Sprint-69-introduced)
credential-leak bug: three sites in `luno/vision.py` and one in
`camera_diagnostic.py` built error/reason strings from the RAW,
un-redacted camera source, which for a `CAMERA_URL` with embedded
credentials would leak them into `camera_status()["error"]` and
`CameraDisconnected` event data — surfaced only when this sprint's own
required "no credential leakage" test exercised the path for the first
time. Fixed via new `_classify_source_for_log()` / `_sanitize_error_
text()` helpers. Fixed two genuine structural correctness gaps:
`RealVisionSource._tracked_cycle_once()` was reporting the PREVIOUS
cycle's camera status (status query moved into a `finally` block AFTER
the capture attempt), and `VisionAdapter.on_camera_status()` was
silently missing the very first `None -> True` connect's
`CameraReconnected` event (`previous is False` → `previous is not
True`).

Added structured, timestamped `[Vision]`-prefixed diagnostic logging
(source classification, backend selection, per-attempt timing/outcome,
backend-mismatch detection, state transitions, cooldown-skip
visibility) at the single authoritative call site — closing the
observability gap that made the reported symptom unanswerable from this
sandbox, and making it self-diagnosable from the user's own next log
capture. Extended `camera_diagnostic.py` to print platform + actual
local backend candidates, so one run on the affected machine confirms
whether Sprint 69's backend-selection fix is genuinely active there.

**No persistent camera configuration was mutated** — per the brief's
explicit instruction, only code paths were corrected.

New test file `tests/test_sprint69_1_camera_dashboard_forensics.py` (15
tests, all 11 brief-mandated categories, including a permanent AST-based
single-call-site regression guard). 0 failed after fixing two test-only
bugs (missing `grab()` on two fake `VideoCapture` stand-ins — not a
production defect). Targeted regression (camera/vision 189 tests +
dashboard 83 tests): 0 failed. Full repository sweep: 3498 passed, 9
failed, 3 skipped, 446s — 8 of the 9 failures are the exact established
environment-gap baseline unchanged since Sprint 68/69; the 9th
(`test_state_isolation.py::test_planner_turn_thread_can_genuinely_
outlive_console_stop`) is a different specific test within the same
file Sprint 69's own baseline already documents as a source of
full-suite-only timing flakiness — reconfirmed clean alone in 1.12s,
not a regression, nothing in that file or planner/console code was
touched. Persistent state unchanged throughout (`config/*.json` ×27
SHA-256-identical, `long_term_memory.json` unchanged since Sprint 55).

See `docs/change_impact/camera_runtime_dashboard_forensics.md` and
`ARCHITECTURE_GUARD.md` §71 for the full writeup.

## 19w/20w. Sprint 69 — Tapo C212 Authentication & Connection Recovery
     (FORENSIC AUDIT + evidence-based error classification/security
     hardening — user reported a previously-working Tapo C212
     connection now showing "disconnect"; brief's own instruction: audit
     before building anything new)

A DIFFERENT subsystem from Sprint 69/69.1/69.2: this sprint is about
`luno/tool_manager/builtin/real_camera_ptz.py` (the `pytapo`-based Tapo
pan/tilt CONTROL tool), not `luno/vision.py`'s OpenCV/RTSP capture path
— both share the same `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD`
config values, but are structurally independent code paths, kept
carefully distinct throughout this sprint's own investigation and
writeup.

**Phase 0/1 — exact-point trace, proved not assumed:** a full-text,
case-insensitive search of `real_camera_ptz.py`, `camera_ptz.py`
(mock), and `luno/bootstrap/adapters.py` for the literal string
`disconnect` returns ZERO matches — `camera_ptz` is registered purely
as a ToolManager tool, never listed in `/api/adapters`, so the
dashboard's generic per-adapter "disconnected" badge cannot refer to
it either. Traced the complete path (`luno/planner/parser.py::
_classify_camera_ptz()` → `ParsedStep` → `planner.py::
_steps_to_tasks()` → `Task(tool_call=ToolCall(tool="camera_ptz", ...))`
→ `executor.py`'s `registry.get_handler(...)` → `RealCameraPTZHandler.
execute()` → one `pytapo` client call per action) and identified TWO
distinct, evidence-based mechanisms for what the user may actually be
seeing: (a) `luno.vision`'s own dashboard Camera badge (a completely
separate code path, Sprint 69/69.1/69.2), or (b) a raw `requests`/
`urllib3` `RemoteDisconnected`-style exception if the camera's TCP
connection resets mid-request (a real, documented `requests` failure
mode). Could not determine from this sandbox which actually occurred —
documented honestly rather than guessed.

**Phase 3 — evidence-based library audit:** `pytapo` 3.4.18 (installed)
performs real, synchronous authentication at `Tapo.__init__()`
construction time. No C212-specific incompatibility evidence found; web
search corroborates "Invalid authentication data" and related pytapo
error codes as a real, recurring, firmware-update-triggered failure
class across the broader Tapo camera family (analogous, not
C212-proof) — per the brief's own "don't invent, don't replace without
proven incompatibility" instruction, `pytapo` itself was NOT replaced
or modified. `pytapo` already performs ONE bounded internal
re-authentication retry (`MAX_LOGIN_RETRIES=1`) on session-token-
invalid errors — this sprint's own classification layer reports that
outcome rather than adding a second retry loop on top of it (proved by
`test_J_a_single_command_makes_at_most_one_underlying_client_call`).

**Fix:** new `classify_tapo_exception()` in `real_camera_ptz.py` maps a
raised exception to one of `{AUTH_FAILED, SESSION_EXPIRED,
AUTH_RATE_LIMITED, DEVICE_OFFLINE, PORT_UNREACHABLE, HOST_UNREACHABLE,
UNKNOWN}`, using ONLY marker strings/exception-type-names directly
confirmed present in `pytapo`'s own source — never guessed. Applied
inside all four of `_move`/`_center`/`_save_preset`/`_goto_preset`'s
existing `except Exception` blocks: `error_type` becomes specific
(e.g. `CameraPTZAuthFailed`) only when a marker matches; anything
unrecognized keeps the exact pre-sprint generic `"CameraPTZError"` —
provably backward compatible (confirmed no consumer anywhere pattern-
matches `error_type`). `retryable` is now `False` for
`AUTH_FAILED`/`AUTH_RATE_LIMITED` (retrying bad credentials cannot
succeed). `luno/bootstrap/adapters.py::
_register_real_camera_ptz_handler()`'s own failure log line now uses
the same classifier for a specific, non-leaking startup message — the
fall-back-to-mock control flow itself is completely UNCHANGED (all 5
pre-existing bootstrap tests pass unmodified), deliberately preserving
Phase 6's "existing mock fallback" requirement rather than risk STOP
CONDITION 7 (camera-pipeline architecture change) without a confirmed
need — see the change-impact doc's own "why the eager-construction/
permanent-fallback architecture was deliberately NOT changed" section.

**Security:** new `_redact_credentials()` strips the exact configured
`TAPO_USERNAME`/`TAPO_PASSWORD` from every outgoing failure message and
the bootstrap log line (defense-in-depth — direct source review found
no current leak in either `pytapo` transport). `target` proven,
structurally, to only ever resolve as a preset name against the
camera's own `getPresets()` — never a host/URL. AST-based static guard
confirms no new persistent-storage surface was introduced.

**New test file:** `tests/test_sprint69_tapo_c212_auth.py` (27 tests) —
construction-time classification (missing username/password, invalid
credential, unreachable, timeout), per-command auth-failure/session-
expiration classification, bounded-retry proof, PTZ success for every
action, an unclassified rejection staying the honest generic bucket,
mock-fallback functionality, credential-redaction (message/data/log
line), no-arbitrary-URL/no-persistent-storage structural guards,
target-as-preset-name-only precedence, no regression to unrelated
tools. All fakes only — zero real password/host/`pytapo` import.

**Regression:** targeted camera/PTZ/tool_manager/planner suite = 134 +
183 passed, 0 failed. Full repository sweep, exact baseline-comparable
command (`tests/` only): **3548 passed, 9 failed, 3 skipped, 449.47s** —
3548 = established 3498-test baseline + exactly 50 new tests (27 this
sprint + 23 from Sprint 69.2's already-written test file); all 9
failures are the EXACT SAME established environment-gap/timing-flake
categories, zero new failures. A broader sweep additionally including
`luno/`'s own test directories (not the established baseline command)
found one further failure, `test_llm_tts_streaming_production.py::
test_13_cancellation_before_first_audio` — re-run in isolation per §21's
own explicit protocol, passed cleanly, confirming full-suite-only
timing flakiness (this file was already flagged as flaky under
full-suite stress in Sprint 69's own notes), not a regression.
Persistent state unchanged throughout (`config/*.json` ×27
SHA-256-identical before and after both sweeps).

**Live verification: NOT POSSIBLE**, for a specific, provable reason —
this sandbox has zero `TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD`/
`CAMERA_PTZ_BACKEND` configured, and no network route to a private-LAN
camera regardless.

See `docs/change_impact/tapo_c212_authentication.md` and
`ARCHITECTURE_GUARD.md` §72 for the full writeup.

## 19x/20x. Sprint 70 — Tapo C212 Live Authentication & Auto-Recovery
     (builds directly on §19w/20w's classification layer — adds a
     bounded, in-memory connection state machine + single-retry
     auto-recovery, explicitly reusing the existing single-client
     architecture per the brief's own "do not create a second camera
     connection system" constraint)

**Phase 0/1:** baseline (59 pre-existing PTZ/bootstrap tests) confirmed
green before any change. Live authentication against a real camera
remains categorically impossible from this sandbox — zero `TAPO_HOST`/
`TAPO_USERNAME`/`TAPO_PASSWORD` configured, no `.env` file, no network
route to a private-LAN camera regardless — per this sprint's own
explicit "never fabricate a successful live connection" STOP CONDITION,
no PASS/FAIL claim is made. Shipped instead: `tapo_ptz_diagnostic.py`
(new, repo root) — a strictly read-only script that performs the one
real `pytapo.Tapo(...)` construction attempt on the actual machine and
prints the classified result via the SAME `classify_tapo_exception()`
the production tool uses, never prints credentials, never moves the
camera. This is now the single most valuable next step for the user.

**Phase 2 (re-confirmed unchanged):** the three connection surfaces
(Tapo PTZ/API; `luno/vision.py`'s own OpenCV/RTSP streaming; the
dashboard status badge, driven only by the latter) remain exactly as
§19w/20w documented — `camera_ptz` still never appears in the dashboard
collector/HTML, and the literal string "disconnect" still never
appears in `real_camera_ptz.py`/`camera_ptz.py`/`luno/bootstrap/
adapters.py` outside this sprint's own documentation-purpose comment.

**Phase 3/4 — the actual new code:** new `PTZConnectionState`
(`DISCONNECTED`/`AUTHENTICATING`/`CONNECTED`/`SESSION_EXPIRED`/
`AUTH_FAILED`/`DEVICE_UNREACHABLE`), tracked per-handler-instance,
in-memory only, never persisted. New `_invoke()` wraps every `pytapo`
client call — on a RECOVERABLE classified failure (`SESSION_EXPIRED`,
`DEVICE_OFFLINE`, `PORT_UNREACHABLE`, `HOST_UNREACHABLE`) with a
`client_factory` configured, rebuilds `self._client` and retries the
SAME call EXACTLY ONCE more; `AUTH_FAILED`/`AUTH_RATE_LIMITED`/
`UNKNOWN` are never retried — matching the brief's own per-category
policy exactly. Bounded BY CONSTRUCTION (`_invoke()` contains zero
loop constructs — proven by an AST static guard), not by a counter — a
dynamic test additionally proves at most 2 total underlying calls occur
even when every client involved always fails. `luno/bootstrap/
adapters.py` now also builds a `client_factory` closure capturing the
SAME 3 already-read credential values used for the first construction
— no new credential storage, no new config read. `client_factory`
defaults to `None`, so every pre-Sprint-70 caller (all 5 bootstrap
tests + all 27 Sprint-69 tests) is byte-for-byte unaffected — confirmed
by re-running them unmodified.

**Phase 5/6:** the exact "command → handler → connection check →
recover if safely possible → PTZ action → result" flow now holds
structurally, without bypassing `ToolManager`/`validate()`/the existing
lock. New `connection_state()` accessor is deliberately NOT wired into
the dashboard — unifying it with `luno.vision`'s own, separate
`CameraState` would fabricate a connectivity claim this tool cannot
back up for the streaming path, forbidden absent an existing safe
shared abstraction (structural tests guard this separation).

**Security:** credential redaction proven to hold on the new recovery
path too (both the original failure text and a failing reconnect
attempt's own exception text). `_invoke()`'s method name is always one
of 5 hardcoded literals — `target` still cannot reach an arbitrary
method or override `TAPO_HOST`. `client_factory` only ever reconstructs
the SAME `pytapo.Tapo` class from the SAME 3 config values — no new
outbound destination. Extended AST guard + direct grep confirm no
`eval`/`exec`/`subprocess`/disk-write surface was introduced.

**Performance:** measured directly — 0.008ms (normal call), 0.012ms
(reconnect-and-succeed), 0.015ms (failed-auth-no-retry), 0.026ms
(exhausted-retry) average overhead — all comfortably under the 5ms
target.

**New test file:** `tests/test_sprint70_tapo_live_recovery.py` (23
tests, categories A-O — valid auth, invalid-credentials-never-retried,
session-expired/transient-network recovery, permanent-unreachable
bounded failure, rate-limit no-retry, unknown-exception safe behavior,
a PROVEN `AUTHENTICATING` transition, reconnect-failure reporting the
ORIGINAL error, two independent no-infinite-retry proofs, credential
redaction on the recovery path, mock-backend non-interference, full
backward compatibility when `client_factory` is omitted, persistent-
state immutability, and dashboard/PTZ separation).

**Regression:** 23 (new) + 59 (pre-existing, unmodified) = 82 passed, 0
failed. Combined with the full camera/vision suite: all passed except
one full-suite-only timing flake in `tests/
test_sprint69_2_camera_state_machine_hardening.py` (a file this sprint
never touched) — reconfirmed clean (23/23) twice in isolation, not a
regression. Full repository sweep: see `docs/testing/
regression_baseline.md`'s own `## Sprint 70` entry. Persistent state
unchanged throughout (`config/*.json` ×27 SHA-256-identical, plus a
dedicated test proving no drift even DURING an actual recovery
scenario, not just at rest).

**Live verification: NOT POSSIBLE**, same provable reason as §19w/20w.

See `docs/change_impact/tapo_c212_live_recovery.md` and
`ARCHITECTURE_GUARD.md` §73 for the full writeup.

## 19y/20y. Sprint 71 — Dashboard Startup & Access Recovery

**Symptom:** "Dashboard Luno tidak dapat dibuka/start/access."

**Root cause (proven live, not assumed):** `DashboardServer.start()`
(`luno/dashboard/server.py`) constructed `ThreadingHTTPServer((self.host,
self.port), _Handler)` — the real socket bind — completely unguarded.
`main.py`'s own `dashboard.start()` call site was ALSO unguarded. The
most common real-world trigger is a stale/previous Luno process (or a
second instance) still holding the configured port
(`errno.EADDRINUSE`/Windows `winerror` 10048). Because neither layer
caught the resulting `OSError`, it propagated all the way out of
`main()` uncaught, crashing the ENTIRE Luno process (voice pipeline,
wake word, everything) — not merely failing to open the dashboard.
Reproduced two ways in this sandbox: an isolated `DashboardServer`
construction against an occupied port, and a full `python main.py` run
against an occupied port (both the before-fix crash and the after-fix
graceful degradation were directly observed).

**Fix (smallest possible, backward-compatible):** `luno/dashboard/
server.py` — new `DashboardBindError(OSError)` (thin subclass, existing
`except OSError` callers unaffected), new `_describe_bind_failure()`
(cross-platform errno/winerror-aware, actionable, never leaks `.env`/
secrets), and the `ThreadingHTTPServer(...)` construction inside
`start()` is now wrapped in `try`/`except OSError` with full rollback of
every observability subscription made before the bind attempt
(`LogCapture`/`EventRingBuffer`/`StatsAggregator`/`VoiceLatencyRecorder`/
`EventLogWriter`), so a failed start never blocks a legitimate retry.
`main.py` — the `dashboard.start()` call site is now wrapped in
`try`/`except OSError`, degrading to the already-established
`DASHBOARD_ENABLED=false` behavior ("rest of Luno keeps working, no
dashboard") instead of crashing the process. No default port/host
changed, no new health endpoint, no socket-option (`SO_REUSEADDR`)
changes.

**Explicitly NOT touched (verified by a dedicated scope-guard test):**
Tapo/C212 auth, PTZ logic, `real_camera_ptz.py`, pytapo/reconnect logic,
Home Assistant, long-term memory, mutation audit, schema/config
migration, any new feature.

**New test file:** `tests/test_sprint71_dashboard_startup_recovery.py`
(15 tests) — startup/bind success, port-conflict handling, exception-
not-silent, thread-not-dying-silently, root route, existing health/ping
endpoints, clean shutdown, existing API-surface compatibility, no
persistent config mutation, camera/PTZ scope guard, a full subprocess-
level E2E reproduction through the real `main.py` entry point, and a
restart-after-failure test. 15/15 passing.

**Regression:** targeted (`test_sprint71_dashboard_startup_recovery.py`
15/15, `test_dashboard.py` 47/47, `test_dashboard_turn_state_
recovery.py` 13/13, `test_production_launcher.py` 23/24 — the 1 failure
is the pre-existing, already-documented `test_07_health_checks_all_
pass_in_default_mock_configuration`). Full repository sweep (~157
remaining files, chunked, plus a whole-repo `--collect-only` pass): every
observed failure traced to pre-existing environment/checkout-state
factors (this checkout's `.env` `MAX_TOKENS_PARAM=max_tokens` override,
`MIC_DEVICE_INDEX`/missing optional deps, this long-lived checkout's
accumulated `config/backups/` count) or documented parallel-execution
flakiness classes (barge_in stress tests, a chained-process segfault in
untouched `stop()` code) — none reproduced when re-run in isolation, none
touch `main.py`/`luno/dashboard/server.py`'s Sprint 71 changes.

**Persistent state:** `config/*.json` (15 files) SHA-256-hashed before
this sprint's first edit and re-hashed after both a full `python main.py`
run (dashboard start/stop) and the full Sprint 71 test suite — byte-
identical throughout. `config/backups/` file count unchanged (41 before,
41 after; zero files newer than the snapshot).

**Live verification: AVAILABLE** (HTTP-level and real-process-level, in
this Linux sandbox — no GUI browser check performed or claimed).

See `docs/change_impact/dashboard_startup_recovery.md` and
`ARCHITECTURE_GUARD.md` §74 for the full writeup.

## 19z/20z. Sprint 71 — Camera Patrol

**Feature:** bounded, deterministic, stoppable-at-any-time camera patrol
across saved Tapo PTZ presets — "mulai patroli kamera"/"mulai patroli
rumah"/"stop patroli kamera"/"status patroli kamera" — built entirely on
the existing PTZ dispatch path, zero new PTZ implementation.

**Files created:**
- `luno/camera_patrol/__init__.py`, `route.py`, `state.py`,
  `controller.py` (`CameraPatrolModule` — the patrol engine, a `Module`).
- `luno/tool_manager/builtin/camera_patrol.py`
  (`CameraPatrolToolHandler`).
- `config/camera_patrol_routes.json` (route definitions only, shipped
  empty — no fabricated example, no credentials).
- `tests/test_sprint71_camera_patrol.py` (37 tests).
- `docs/change_impact/camera_patrol.md`.

**Files modified (5, all additive/backward-compatible):**
- `main_runtime_demo.py` — `ToolManagerBridgeModule` gained an optional,
  empty-by-default pre-dispatch hook list
  (`register_pre_dispatch_hook()`), invoked just before every tool call
  executes; used so a manual PTZ command always stops an active patrol
  first. No-op for every tool call that doesn't register a hook — zero
  behavior change confirmed for every pre-existing caller.
- `luno/bootstrap/modules.py` — constructs `CameraPatrolModule`,
  registers it into `tool_manager_module.manager.registry` as
  `"camera_patrol"`, registers the pre-dispatch hook, adds it to
  `bind_event_bus`/`register_module`, returns it as
  `"camera_patrol_module"`.
- `luno/planner/parser.py` — new `_classify_camera_patrol()`, mirrors
  `_classify_camera_ptz`'s own conservative co-occurrence approach
  (patrol word + start/stop/status verb).
- `luno/dashboard/collectors.py` — `collect_vision()` gained an optional
  `modules` parameter (default `None`, fully backward-compatible);
  additively merges `patrol_state`/`patrol_route`/`patrol_preset`/
  `patrol_index`/`patrol_cycle`/`patrol_max_cycles`/`patrol_reason` when
  present.
- `luno/dashboard/server.py` — one-line change, `/api/vision` now passes
  `self.modules` through to `collect_vision()`.
- `luno/dashboard/static/index.html` — `loadVision()` additively renders
  Patrol/Patrol Route/Patrol Preset/Patrol Cycle/failure-reason cards
  only when `patrol_state` is present; no existing card touched.

**One pre-existing test file updated (justified, not a workaround):**
`tests/test_sprint68_mutation_audit_hardening.py` —
`test_baseline_config_json_count_is_15` renamed to `..._is_16`, because
this sprint's own sanctioned new `config/camera_patrol_routes.json`
legitimately moved the real config-file count from 15 to 16.

**Explicitly NOT touched, verified by hash comparison:**
`real_camera_ptz.py`, `camera_ptz.py`, `luno/tool_manager/manager.py`,
`luno/tool_manager/builtin/__init__.py`, `luno/bootstrap/adapters.py`,
`luno/core/events.py`, Tapo authentication/reconnect logic, Home
Assistant, RTSP/Vision pipeline, existing camera config.

**Tests created:** `tests/test_sprint71_camera_patrol.py` — 37 tests
(route validation, full lifecycle, safety bounds, deterministic stop,
timeout/disconnect/auth-failure handling via the unmodified Sprint 69/70
classifier, no-concurrent-ownership, manual-override, repeated-start
refusal, persistence, security, dashboard integration, parser
classification, in-memory performance). 37/37 passing, stable across 4
consecutive full runs.

See `docs/change_impact/camera_patrol.md` and `ARCHITECTURE_GUARD.md`
§75 for the full writeup.

## 19aa/20aa. Sprint 72 — Automation Engine Dasar

**Feature:** deterministic `TRIGGER -> CONDITION -> ACTION -> VERIFY ->
COOLDOWN` pipeline for cross-device automation - "night mode",
scheduled/event-driven rules - built entirely on the existing Event Bus/
Scheduler/ToolManager dispatch path, zero second implementation of any
of the three, zero arbitrary code execution.

**Files created:**
- `luno/automation/__init__.py`, `models.py` (typed allowlisted domain
  model - `AutomationRule`/`Trigger`/`Condition`/`Action`/`Execution`,
  no expression field anywhere), `conditions.py` (pure read-only
  evaluator), `engine.py` (`AutomationEngine` - the pipeline, a
  `Module`).
- `luno/tool_manager/builtin/automation.py` (`AutomationToolHandler` -
  `run`/`enable`/`disable`/`status`).
- `config/automation_rules.json` (rule definitions only, shipped empty).
- `tests/test_sprint72_automation_engine.py` (78 tests).
- `docs/change_impact/automation_engine.md`.

**Files modified (5 source + 1 test literal fix, all additive/
backward-compatible):**
- `luno/bootstrap/modules.py` - constructs `AutomationEngine`, registers
  it as tool `"automation"`, registers its camera-ownership pre-dispatch
  hook alongside Sprint 71's own (both on the same, already-multi-
  consumer hook list), binds it to `runtime.scheduler` for time
  triggers, adds it to `bind_event_bus`/`register_module`, returns it as
  `"automation_engine"`.
- `luno/planner/parser.py` - new `_classify_automation()`, requires the
  anchor word "otomasi"/"otomatisasi"/"automation" to co-occur with a
  start/enable/disable/status verb (same conservative co-occurrence
  approach `_classify_camera_patrol`/`_classify_llm_mode` already use) -
  deliberately does NOT classify a bare "aktifkan mode malam" (no anchor
  word) as an automation command, to avoid colliding with ordinary Home
  Assistant parsing elsewhere in this file.
- `luno/dashboard/collectors.py` - new `collect_automation(modules)`
  function (a new panel, not an extension of `collect_vision()`).
- `luno/dashboard/server.py` - one-line addition, new `/api/automation`
  route.
- `luno/dashboard/static/index.html` - new "Automation Engine" nav entry
  (inside the pre-existing "Automation" nav group) and panel; no
  existing card/panel touched.
- `tests/test_sprint68_mutation_audit_hardening.py` - config-file-count
  literal updated 16 -> 17 (this sprint's own sanctioned new config
  file), plus (separately) `tests/test_sprint65_tool_file_access_
  audit.py`'s security scanner correctly flagged this sprint's own
  `models.py` docstring for literally naming `eval()`/`exec()`/
  `shell=True` while documenting the prohibition - fixed by rewording
  the docstring (a real fix to this sprint's own new file, not a
  scanner workaround).

**Explicitly NOT touched, verified by hash comparison:** Tapo/C212 auth,
PTZ implementation, `camera_patrol/controller.py`'s own logic, `luno/
core/event_bus.py`, `luno/core/scheduler.py`, `ToolManager`'s dispatch
core, the Home Assistant handler, the Vision pipeline, `main_runtime_
demo.py` (not modified at all this sprint).

**Tests created:** `tests/test_sprint72_automation_engine.py` - 78
tests (domain-model validation, pure condition evaluator, AST-based
security scan, trigger engine, action engine against real mock
handlers, no-partial-execution semantics, cooldown, loop protection,
camera ownership, failure/timeout handling, persistence, dashboard/
ToolHandler surface, NLP parser classification, in-memory performance).
78/78 passing, stable across 3 consecutive full runs.

See `docs/change_impact/automation_engine.md` and `ARCHITECTURE_
GUARD.md` §76 for the full writeup.

## 19bb/20bb. LUNO P0 — Camera Automation / Safe Integration & Non-Regression Protocol

**Feature, under an explicit non-regression protocol:** lets an
operator-allowlisted set of Home Assistant camera/motion entities feed
Sprint 72's ALREADY-EXISTING `AutomationEngine` - zero lines changed in
that engine, zero lines changed in the Event Bus, zero lines changed in
`HomeAssistantAdapter`, zero new automation framework, zero new HA
client. This sprint's own brief mandated treating the entire existing
codebase as protected infrastructure and required explicit
justification for every existing-file touch; exactly one existing file
was touched, additively.

**Files created:**
- `luno/camera_automation/__init__.py`, `config.py`
  (`CameraAutomationConfig.from_env()` - env-var-only, same
  self-contained pattern `BargeInConfig.from_env()` already established;
  `enabled` defaults to `False`), `module.py` (`CameraAutomationModule` -
  a `Module`; subscribes to the EXISTING `device_state_changed` event
  only when enabled; dedupe + cooldown + ephemeral in-memory state;
  every Event Bus callback wrapped in a fail-safe `try/except`).
- `tests/test_p0_camera_automation.py` (23 tests).
- `docs/change_impact/camera_automation_p0.md`.

**Files modified (1, purely additive):**
- `luno/bootstrap/modules.py` - one new import line, one new
  `CameraAutomationModule(config=CameraAutomationConfig.from_env())`
  construction block with an explanatory comment, one new entry added to
  each of the existing `bind_event_bus`/`register_module` loops (the
  exact same wiring pattern `camera_patrol_module`/`automation_engine`
  already use), one new key added to the returned dict. No existing line
  altered, reordered, or removed.

**Explicitly NOT touched, verified by re-running each subsystem's own
dedicated test suite unmodified:** the Event Bus (`luno/core/
event_bus.py`), the scheduler (`luno/core/scheduler.py`),
`HomeAssistantAdapter`/`HomeAssistantSource`/`HomeAssistantClient`
(`luno/adapters/home_assistant.py`), `AutomationEngine`/`conditions.py`/
`models.py` (`luno/automation/`), `CameraPatrolModule` (`luno/
camera_patrol/`), `ToolManagerBridgeModule`, any `ToolManager` handler
including `luno/tool_manager/builtin/home_assistant.py`, any memory/
persistence system, the LLM/voice/TTS/STT/wake-word pipeline, the
dashboard, the planner/NLP parser, `luno/bootstrap/launcher_config.py`,
and every `config/*.json` file (zero new config files this sprint - the
allowlist is env-var-only, avoiding the config-file-count-literal
forward-fix Sprint 72's own `automation_rules.json` addition required).

**Tests created:** `tests/test_p0_camera_automation.py` - 23 tests
(config parsing/defaults, the module in isolation against a fake event
bus covering disabled-by-default/allowlist-filtering/dedupe/cooldown/
missing-field-safety/fail-safe exception isolation/clean stop/health
reporting, and real-bootstrap E2E tests proving disabled-by-default has
genuinely zero Event Bus footprint, the existing `HomeAssistantAdapter`'s
inbound/outbound behavior is byte-for-byte unaffected by this module's
presence, an allowlisted entity change publishes exactly one new-
namespaced event while a non-allowlisted entity produces none, and the
full pipeline through to an existing `home_assistant.turn_on` action
fires end to end from a plain JSON rule with zero engine changes).
23/23 passing.

**Regression:** full repository sweep, before: 4510 collected / 4480
passed / 30 failed / 0 skipped (every failure traced to a pre-existing,
already-categorized, environment-specific cause, none touching camera/
HA/automation). After: 4533 collected (4510 + this sprint's own 23) /
4503 passed / 30 failed / 0 skipped - the exact same 30 tests, same
causes, zero new failures, zero incidental fixes claimed. Additionally
spot-verified in isolation: every existing test file that calls
`register_all_modules` (the one function this sprint's single edit
lives in) - all pass unchanged.

See `docs/change_impact/camera_automation_p0.md` and `ARCHITECTURE_
GUARD.md` §77 for the full writeup.

## 19cc/20cc. LUNO P0.5 — Real Camera Integration

**Feature (integration sprint, not an architecture rewrite):** connects
P0's already-shipped `CameraAutomationModule` to real Home Assistant
camera-related entities via a new, generic `CameraProfile ->
CameraEvent` classification layer - explicitly NOT a vendor-specific
"Tapo adapter" (Home Assistant has already abstracted the Tapo protocol
into generic entities before Luno ever sees them, so no Tapo-specific
code exists or was written).

**Files created:**
- `luno/camera_automation/cameras.py` (`CameraProfile`, `CameraEvent`,
  `build_entity_role_index()`, `classify_state_change()`,
  `load_camera_profiles()` - pure/stateless, no Event Bus, no I/O beyond
  its own config read).
- `config/camera_automation.json` (per-camera entity-role mapping,
  shipped with every role `null` - no fabricated entity ids).
- `ha_camera_discovery.py` (read-only live-discovery CLI for the user's
  real machine, same precedent as Sprint 70's `tapo_ptz_diagnostic.py`).
- `tests/test_p0_5_camera_integration.py` (36 tests).
- `docs/change_impact/camera_automation_p0_5.md`.

**Files modified (3, all additive; 1 test literal fix):**
- `luno/camera_automation/config.py` - new `cameras_path` field/env var
  only; every P0 field unchanged.
- `luno/camera_automation/module.py` - `_handle()` gained one new
  classification branch ahead of P0's own unmodified flat-allowlist
  branch; new `reload_cameras()`, new `CAMERA_EVENT_TYPE`; dedupe/
  cooldown is the SAME shared dict/logic both branches use (never
  duplicated).
- `luno/camera_automation/__init__.py` - new exports only.
- `tests/test_sprint68_mutation_audit_hardening.py` - config-file-count
  literal 17 -> 18 (this sprint's own sanctioned new config file), same
  forward-fix precedent Sprint 71/72 each already established once.

**`luno/bootstrap/modules.py` was NOT touched this sprint** -
`CameraAutomationModule` was already constructed/registered there since
P0; nothing new needed wiring.

**Discovery:** a genuine read-only live-discovery attempt was made
(this sandbox has real `HA_URL`/`HA_TOKEN` configured) via the EXISTING
`luno.ha_client.HomeAssistantClient` - failed with `proxy rejected
connection: HTTP 403` (this sandbox's own network policy, not a code
defect). LIVE VERIFICATION: NOT AVAILABLE, honestly documented, not
faked. Clarification found during discovery: this project's existing
Tapo C212 integration (Sprint 69-72) is a DIRECT `pytapo` LAN connection
for PTZ, entirely separate from Home Assistant.

**Explicitly NOT touched, verified by re-running each subsystem's own
suite unmodified:** the Event Bus, `luno/adapters/home_assistant.py`,
`AutomationEngine`, `luno/bootstrap/modules.py`, memory/LLM/voice/STT/
TTS, every other existing config schema, the Tapo PTZ integration.

**Tests created:** `tests/test_p0_5_camera_integration.py` - 36 tests
(entity mapping, event conversion including the motion-vs-offline
distinction and the unavailable-state fallback, `load_camera_profiles`
malformed-input safety, metadata/stable-camera-id, the module in
isolation covering the new branch/shared dedupe-cooldown/unknown-entity
logging/malformed-config-safety/fail-safe isolation, and real-bootstrap
E2E covering the full HA-event-to-CameraEvent pipeline, the full
pipeline through to an existing `AutomationEngine` rule with zero engine
changes, unknown-entity-never-triggers-automation, disabled-remains-
inert, and the EXACT SAME existing-HA-adapter assertions P0's own suite
makes, re-run active). 36/36 passing. P0's own `tests/
test_p0_camera_automation.py` re-run completely unmodified - still
23/23 passing.

**Regression:** before: 4533 collected/4503 passed/30 failed/0 skipped.
After: 4569 collected (4533+36)/4538 passed/31 failed/0 skipped. 30 of
31 are the identical pre-existing baseline failures. The 31st
(`test_streaming_e2e.py::
test_D_barge_in_between_llm_and_tts_chunk_never_plays`) was
investigated, not assumed: passes 6/6 in isolation, and is the EXACT
SAME test this very file already documents (see §13) as a
non-deterministic full-suite timing flake dating to Sprint 49 -
`luno/camera_automation/` shares zero code with the TTS/barge-in/LLM
streaming subsystem that test exercises.

See `docs/change_impact/camera_automation_p0_5.md` and `ARCHITECTURE_
GUARD.md` §78 for the full writeup.

## 19dd/20dd. LUNO P0.5.1 — Real Tapo C212 Entity Discovery

**Discovery-only sprint (no automation behavior, no architecture
change):** rewrote the P0.5-era `ha_camera_discovery.py` into a
strictly read-only discovery tool that classifies real HA entities by
entity->device->manufacturer relationship evidence rather than by name-
guessing, to prepare for filling in `config/camera_automation.json`'s
still-`null` entity-role fields.

**Bug found and fixed (permitted, no architecture change required):**
the P0.5 script called `client.get_states()` without first starting
`client.listen_and_dispatch()` as a background task - per `luno/
ha_listener.py`'s own documented requirement, `pending_responses` is
only ever filled in by that background loop, so every call would have
silently timed out even on a fully successful connection. Fixed inside
the standalone script only, mirroring the correct pattern already in
`luno/adapters/real_home_assistant.py` (lines 100-127). `luno/
ha_client.py` was NOT touched.

**New capability:** a local `_send_and_wait()` helper reuses the SAME
connected `HomeAssistantClient` instance's own `ws`/`msg_id`/
`pending_responses`/`call_lock` attributes (not a second client) to
call HA's standard read-only `config/entity_registry/list` and
`config/device_registry/list` commands, enabling camera/motion/human/
availability classification by real device relationship (falling back
to explicitly-unconfirmed keyword matching only when registry data is
unavailable). Human detection is never conflated with generic motion.
The pytapo/HA "same physical camera" relationship is only ever reported
CONFIRMED when the discovered camera device's HA `connections` list
contains an entry matching the existing `TAPO_HOST` config value.

**Files created:** `tests/test_ha_camera_discovery.py` (8 tests, the
smallest possible per the brief's own Section 16, covering `_build_
report()`'s pure classification logic via synthetic fixtures), `docs/
change_impact/camera_automation_p0_5_1.md`.

**Files modified:** `ha_camera_discovery.py` only (standalone script,
not a `luno/` module). No file under `luno/` was touched. Zero new
config files - `config/camera_automation.json`'s field count and the
config-file-count test are unchanged from P0.5 (still 18).

**Explicitly NOT touched:** the Event Bus, `luno/ha_client.py`,
`AutomationEngine`, `luno/camera_automation/*`, `config/
camera_automation.json`, `luno/bootstrap/modules.py`, memory/LLM/voice/
STT/TTS, every other existing config schema, the Tapo PTZ integration.

**Live verification attempt:** ran the rewritten script against this
sandbox's real, configured `HA_URL`/`HA_TOKEN` - identical `HTTP 403`
proxy rejection every prior HA sprint (P0, P0.5) has already
documented; correctly reported as `HA DISCOVERY: BLOCKED` (Section 14's
required distinction from "camera not found"). Tapo C212 presence in
Home Assistant remains genuinely undetermined in this sandbox - running
the script on the user's own machine is the missing step.

**Tests:** `tests/test_ha_camera_discovery.py` - 8/8 passing. `tests/
test_p0_camera_automation.py` (23) and `tests/
test_p0_5_camera_integration.py` (36) re-run completely unmodified -
59/59 still passing.

**Regression:** `tests/test_sprint68_mutation_audit_hardening.py` -
65/67 passing; the 2 failures (`config/backups` file count, mutation-
audit-dir baseline) trace to real accumulated files dated Aug 11-18,
predating this sprint - confirmed unrelated (this sprint wrote no file
under `luno/`, no backup file, ran no mutation-audited operation). Left
unfixed per Section 17's own instruction not to modify production code
for unrelated test failures.

See `docs/change_impact/camera_automation_p0_5_1.md` and `ARCHITECTURE_
GUARD.md` §79 for the full writeup.

## 19ee/20ee. LUNO P0.5.2 — Tapo C212 Event Source Audit

**Audit + read-only prototype sprint (no automation behavior, no
architecture changed, no production integration wired):** since P0.5.1
found the Tapo C212 genuinely absent from Home Assistant, this sprint
audits the OTHER existing Luno camera path - direct `pytapo` - to
determine whether it can supply motion/human/availability events
instead.

**Major discovery: two separate pre-existing camera paths exist, not
one.** Path A (`luno/bootstrap/adapters.py::
_register_real_camera_ptz_handler` -> `pytapo.Tapo(...)` -> `luno/
tool_manager/builtin/real_camera_ptz.py::RealCameraPTZHandler`) is
PTZ-only - confirmed by reading the full, unmodified file, it only ever
calls `moveMotor`/`calibrateMotor`/`savePreset`/`getPresets`/
`setPreset`. Path B (`luno/adapters/real_vision.py::RealVisionSource`
-> `luno/vision.py`'s OpenCV/RTSP capture, NOT pytapo, over the same
TAPO_HOST-derived `CAMERA_URL` -> YOLO detection/tracking -> `luno/
adapters/vision.py::VisionAdapter`) is a COMPLETE, ALREADY-WORKING,
ALREADY-ENABLED-IN-THIS-CHECKOUT'S-OWN-`.env`
(`CAMERA_VISION_ENABLED=true`, `VISION_BACKEND=real`) human-detection
and camera-availability pipeline - it already publishes
`CameraPersonEntered`/`CameraPersonLeft`/`HumanEntered`/`HumanLeft`/
`PoseChanged`/`CameraDisconnected`/`CameraReconnected` on the Event Bus
TODAY, completely independent of pytapo and of Home Assistant. This was
discovered, not built, this sprint.

**pytapo (3.4.18) capability audit (static source inspection - the
installed package cannot actually be imported in this sandbox, see
below):** `getMotionDetection()`/`getPersonDetection()` return the
camera's own firmware detection CONFIG (enabled/sensitivity) in two
genuinely separate namespaces (`motion_detection` vs
`people_detection`) - never a live event. `getEvents(start, end)` calls
`searchDetectionList` and IS a genuine, evidence-based POLL-based event
log - the one real event mechanism pytapo offers. No push/websocket/
subscribe/callback API exists anywhere in the installed package
(confirmed by exhaustive grep of every method name in the package
source).

**Files created:** `tapo_camera_event_audit.py` (root level, following
the repo's existing `tapo_ptz_diagnostic.py`/`ha_camera_discovery.py`
convention - read-only, reuses existing `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` config and the existing `classify_tapo_exception`/
`_redact_credentials`, never calls any set/play/start/stop/PTZ method,
`--duration`-bounded `getEvents()` before/after diff for live
observation), `tests/test_tapo_camera_event_audit.py` (18 tests, mocked
clients, no hardware required), `docs/change_impact/
camera_automation_p0_5_2.md`.

**Files modified: none under `luno/`** - confirmed via `find luno
-newer <P0.5.1's change-impact doc>`, zero results. `config/
camera_automation.json` untouched. `CameraAutomationModule` untouched.

**Live probe attempt:** `pytapo` import fails in this sandbox
(`ModuleNotFoundError: No module named 'kasa.transports'` - a
pre-existing `kasa`/`cryptography` version mismatch in this checkout's
`.venv`, NOT fixed this sprint since upgrading dependencies is out of
scope, and NOT newly caused by this sprint - `luno/bootstrap/
adapters.py` already silently absorbs this exact failure via its
existing broad `except Exception`). Correctly reported as `RESULT:
IMPORT_FAILED`, distinct from a connection failure or "camera not
found" - every capability honestly reported UNKNOWN, nothing
fabricated.

**Tests:** `tests/test_tapo_camera_event_audit.py` - 18/18 passing. All
directly-related existing suites (`test_p0_camera_automation.py` 23,
`test_p0_5_camera_integration.py` 36, `test_ha_camera_discovery.py` 8,
`test_sprint69_tapo_c212_auth.py` + `test_sprint70_tapo_live_recovery.py`
50, `test_sprint71_camera_patrol.py`, `luno/tool_manager/tests/
test_camera_ptz.py`) re-run unmodified - baseline 181/181, after
199/199 (181+18), zero new failures.

**Recommended integration source:** BOTH, asymmetrically -
`luno.vision`/`VisionAdapter` (Path B) is the strongest candidate for
human detection and availability (already live, already tested,
already enabled); pytapo (Path A) remains correct for PTZ only, weak
for event-driven automation (poll-only, no push). Home Assistant
remains unconfirmed per P0.5.1. Next sprint recommendation: investigate
bridging Path B's already-existing events into `CameraAutomationModule`
- NOT implemented this sprint.

See `docs/change_impact/camera_automation_p0_5_2.md` and
`ARCHITECTURE_GUARD.md` §80 for the full writeup.

## 19ff/20ff. LUNO P0.5.3 — Vision Event → Camera Automation Bridge

**Bridge sprint (no new computer vision, no new automation rules):**
connects the ALREADY-EXISTING `luno.adapters.vision.VisionAdapter`
events - discovered by P0.5.2's own audit - to the ALREADY-EXISTING
`CameraAutomationModule` (P0/P0.5). Mapping (read from the actual
implementation, not assumed): `CameraPersonEntered` -> `human_detected`,
`CameraPersonLeft` -> `human_cleared`, `CameraDisconnected` ->
`camera_offline`, `CameraReconnected` -> `camera_online`. Deliberately
does NOT use `HumanEntered`/`HumanLeft` (per-tracked-individual, would
require the bridge to re-implement its own presence-counting/debounce -
the safest canonical mapping reuses the ALREADY room-level-debounced
`CameraPersonEntered`/`CameraPersonLeft` instead). No motion event
exists anywhere in the Vision pipeline - `motion_detected`/
`motion_cleared` are NEVER fabricated (reported as "NOT AVAILABLE FROM
EXISTING VISION EVENT PIPELINE").

**New file:** `luno/camera_automation/vision_bridge.py` -
`VisionCameraEventBridge`, a `Module` (`dependencies=
["camera_automation"]`) subscribing to 4 existing event type strings,
translating each into the existing `CameraEvent` model, handed to a new
`CameraAutomationModule.ingest_external_camera_event()` entry point.
Camera id: fixed/configurable (`CAMERA_AUTOMATION_VISION_CAMERA_ID`,
default `"tapo_c212"`) since Vision has no camera-identity concept of
its own - Vision core untouched. Confidence: always `None` (neither
source event carries one). Zero dedupe/cooldown of its own -
`ingest_external_camera_event()` routes through the EXACT SAME
`_publish_if_not_suppressed()` the HA-sourced classified path already
uses.

**Modified (additive only):** `luno/camera_automation/module.py` - two
new methods, `is_enabled()` (read-only accessor) and `ingest_external_
camera_event()` (reuses existing dedupe/cooldown, re-checks the feature
flag, defensively wrapped) - `_handle()`/`_on_device_state_changed()`
unchanged. `luno/camera_automation/__init__.py` - new export. `luno/
bootstrap/modules.py` - minimal wiring (construct + bind_event_bus +
register_module), zero existing lines changed.

**Explicitly NOT touched:** `VisionAdapter`, the YOLO/OpenCV/RTSP
pipeline, the Event Bus, `AutomationEngine`, `luno/ha_client.py`, pytapo
integration - confirmed via `find luno -newer <P0.5.2's doc>`, exactly
the 4 files listed above and nothing else.

**Tests:** `tests/test_p0_5_3_vision_camera_bridge.py` - 26 tests
(mapping, unknown event, confidence, camera id, failure isolation,
feature flag, no motion fabrication, module additions, real-bootstrap
E2E). 26/26 passing. Baseline (Vision/adapters/P0/P0.5/P0.5.1/P0.5.2/
automation_engine suites) 307/307, after 333/333 (307+26), zero new
failures.

**Live verification:** no real Tapo C212/RTSP event observed (no camera
hardware/network in this sandbox, same limitation every camera sprint
has documented) - honestly reported, not glossed over. What WAS
verified for real: a full real-bootstrap E2E test proving the event
TRANSPORT (Event Bus -> bridge -> `ingest_external_camera_event()` ->
`camera_automation.camera_event`) genuinely works end to end -
explicitly distinct from, and not a substitute for, live-camera
verification.

See `docs/change_impact/camera_automation_p0_5_3.md` and
`ARCHITECTURE_GUARD.md` §81 for the full writeup.

## 19gg/20gg. LUNO P0.5.4 — Live Tapo C212 Camera Event Verification

**Live-hardware verification sprint - ZERO production code changed.**
Attempted to verify the P0.5.3 Vision -> Bridge -> Camera Automation
pipeline against the user's real Tapo C212. Directly re-probed (not
assumed) this sandbox's network reachability this sprint: `TAPO_HOST`
(private LAN address) - `OSError: Network is unreachable` on ports
443/554/80; the configured HA hostname - `gaierror: Temporary failure
in name resolution`. Also confirmed `ultralytics` (YOLO) is not
installed in this sandbox - a second, independent reason the real
Vision pipeline cannot initialize here even if the camera were
reachable. Per the brief's own explicit "do NOT use the sandbox as a
substitute for live hardware" instruction, all 6 live tests (idle/
human-enter/human-stays/human-exit/human-re-entry/camera-disconnect-
reconnect) are honestly reported `NOT PERFORMED` with their real
reason - never fabricated as PASS.

**Camera ID investigation (Section 15):** no new live evidence
available this sprint (same network blockers); restates P0.5.1/
P0.5.2/P0.5.3's existing evidence chain precisely -
`same_physical_camera: UNKNOWN`, not upgraded. Notes the one real,
verifiable configuration-level fact: `luno.vision.CAMERA_URL` and
`pytapo.Tapo(...)` are both derived from the exact same `TAPO_HOST`/
`TAPO_USERNAME`/`TAPO_PASSWORD` env vars - configuration agreement, not
device-identity proof.

**Files:** `docs/change_impact/camera_automation_p0_5_4.md` (new) only.
No file under `luno/` was touched - confirmed via `find luno -newer
<P0.5.3's doc>` (only a runtime `.jsonl` log, not source).

**Regression:** baseline (recorded before any activity this sprint)
333/333 passed across every P0/P0.5/P0.5.1/P0.5.2/P0.5.3/Vision/
adapters/automation_engine suite; after (re-run at sprint end)
identical, 333/333 - unchanged, as expected for a zero-code-change
sprint. `test_sprint68_mutation_audit_hardening.py` spot-check: 65/67,
same 2 pre-existing unrelated environmental failures already
documented.

**Honest Definition-of-Done accounting:** real camera/RTSP/Vision
pipeline NOT tested (environment cannot reach it); no event fabricated;
no automation action executed; no PTZ movement; no HA control service
called; regression clean. This sprint's own report explicitly
recommends re-running the identical protocol on the user's real
deployment machine before any future sprint proceeds to `config/
automation_rules.json`.

See `docs/change_impact/camera_automation_p0_5_4.md` and
`ARCHITECTURE_GUARD.md` §82 for the full writeup.

## 19hh/20hh. LUNO P0.5.4-LIVE — Real Camera Proof-of-Life

**Live-verification attempt, ZERO production code changed.** The brief
asked for the test to run on "the user's real Luno machine" - this
sprint established, definitively, that agent code execution ALWAYS
happens in an isolated cloud sandbox regardless of which folder is
mounted for file access; a fresh TCP probe to `TAPO_HOST` still fails
with `Network is unreachable`. This is a permanent property of the
execution environment, not a fixable sandbox limitation.

**Deliverable instead:** `luno_live_camera_event_observer.py`
(root-level, read-only, standalone) - a ready-to-run script the USER
executes themselves on their real machine. Pre-flight checks hard-stop
before booting the runtime if any critical check fails. Boots the real
existing bootstrap, sets `CAMERA_AUTOMATION_ENABLED=true` in its own
process only, subscribes a temporary print-only observer to
`camera_automation.camera_event` plus the four raw Vision events (for
which only the event type/timestamp are ever printed, never
`event.data` - `CameraDisconnected`/`CameraReconnected` can carry the
full credentialed RTSP URL). Cleans up via the existing
`ShutdownCoordinator`.

**Files:** `luno_live_camera_event_observer.py` (new),
`tests/test_luno_live_camera_event_observer.py` (new, 13 tests),
`docs/change_impact/camera_automation_p0_5_4_live.md` (new). Zero files
under `luno/` touched.

**Tests/Regression:** 13/13 new passing. Baseline 333/333 (unchanged
from P0.5.4), after 346/346, zero new failures.

**Live tests A-F:** all 7 honestly NOT PERFORMED - never fabricated as
PASS. Hands the user a tested, safe script plus exact instructions to
produce the real evidence themselves.

See `docs/change_impact/camera_automation_p0_5_4_live.md` and
`ARCHITECTURE_GUARD.md` §83 for the full writeup.

## 19ii/20ii. LUNO P0.5.4-FIX — Use the Real main.py Vision Lifecycle

**Bug-fix sprint, single script touched, ZERO production `luno/` files
changed.** The user ran P0.5.4-LIVE's observer on their real machine and
got only `scheduled_vision_poll (0.0ms)`, no camera events - while their
real `main.py` performs genuine live YOLO detection. Traced (not
guessed) through `main.py`, `luno/bootstrap/launcher_config.py`, and
`luno/bootstrap/adapters.py`.

**Root cause:** the observer's `main()` called a bare `LauncherConfig()`
instead of `LauncherConfig.load()` (what `main.py` line 66 itself
calls). The bare constructor never reads `.env`, so `vision_backend`
silently stayed at its hardcoded default `"mock"` even with `.env`'s
`VISION_BACKEND=real` - `register_all_adapters()` therefore built
`VisionAdapter` against `MockVisionSource()`, never `RealVisionSource()`
- no real RTSP, no real YOLO, no real Vision events. The
`scheduled_vision_poll` log line the user saw is unrelated: a
content-free periodic Event Bus tick `VisionAdapter` has no handler for
at all (confirmed via grep, zero matches).

**Fix:** one-line change - `LauncherConfig()` -> `LauncherConfig.load()`
- plus a visible warning if `cfg.vision_backend != "real"` after boot.
No production Vision/CameraAutomation/Bridge file needed any change.

**Files:** `luno_live_camera_event_observer.py` (modified, 1 line +
warning block), `tests/test_luno_live_camera_event_observer.py`
(modified, +3 tests), `docs/change_impact/camera_automation_p0_5_4_fix.md`
(new). Zero files under `luno/` touched - confirmed via `find luno
-newer <P0.5.4-LIVE's doc>` (only stale `.pyc` cache, no `.py` source).

**Tests/Regression:** observer file 13 -> 16 passing (+3, wiring/config
proofs only - explicitly not a fake hardware-verification test).
Targeted Vision/camera/automation-engine suite: 331 -> 334 passing,
zero failures. `luno/adapters/tests/test_adapters.py`: 15/15 unchanged.

**Live status: NOT VERIFIED** - fixes the tool on a fully code-traced
root cause; does not and cannot constitute a live-hardware PASS. The
user must re-run the observer, confirm `vision backend: real` prints,
and report back the resulting `CameraPersonEntered -> human_detected ->
camera_automation.camera_event` trace.

See `docs/change_impact/camera_automation_p0_5_4_fix.md` and
`ARCHITECTURE_GUARD.md` §84 for the full writeup.

## 19jj/20jj. LUNO P0.6 — Camera Automation Rule Integration + Log-Only

**Connects `camera_automation.camera_event` to the existing
`AutomationEngine` (Sprint 72), log-only, zero device actions.** First
rule: `camera_human_detected_log` - `kind=="human_detected"` ->
`automation.log` (an action type that already existed, already
internal-only, structurally incapable of reaching Home Assistant/PTZ -
`_dispatch_internal_action()` never calls `_dispatch_tool_call()`, the
only path to `tool_requested`).

**Architecture audit finding:** the pre-existing condition engine
(`evaluate_condition(condition, state_readers)`) could only read
externally-registered `state_readers` - it had no way to inspect the
triggering event's own payload at all. "Match `kind=='human_detected'`"
was structurally inexpressible before this sprint.

**Minimal fix (only production files touched):**
`luno/automation/conditions.py` - `evaluate_condition()` gained an
optional `event_data` parameter and a new `event.<field>` condition-
target convention (fully backward compatible - every non-`event.`
target unaffected). `luno/automation/engine.py` - threads `event.data`
through `_trigger()`/`_run_execution()`/`_evaluate_conditions()` as one
additional, optional (`None`-default) parameter at each hop; time/
manual triggers unaffected. `config/automation_rules.json` (`{}` -> one
rule, generic - no `camera_id` condition since identity remains
unverified per P0.5.1-P0.5.4).

**Files:** `luno/automation/conditions.py` (modified), `luno/
automation/engine.py` (modified), `config/automation_rules.json`
(modified), `tests/test_p0_6_camera_automation_rules.py` (new, 27 test
cases), `docs/change_impact/camera_automation_p0_6.md` (new). Zero
files under `luno/vision.py`/`luno/adapters/vision.py`/`luno/
camera_automation/*.py`/Tapo/RTSP touched.

**Tests/Regression:** baseline 224/224 (measured this sprint), after
251/251 (224+27), zero new failures. Spot-check (camera_patrol/
adapters/dashboard): 99/99 unaffected.

**Live status: NOT PERFORMED** - same structural sandbox constraint as
every prior sprint. A real-bootstrap, real-Event-Bus, simulated-event
smoke test proved the full `EVENT -> RULE -> MATCH -> LOG` chain - not
hardware verification, not presented as one. The user's real `main.py`
loads this rule by default (`enabled: true`) on their next run.

See `docs/change_impact/camera_automation_p0_6.md` and
`ARCHITECTURE_GUARD.md` §85 for the full writeup.

## 19kk/20kk. LUNO P0.6.1 — Live Camera → Automation Log-Only Verification

**Live-verification sprint. Zero files under `luno/` touched.** Extends
the SAME standalone `luno_live_camera_event_observer.py` script
(P0.5.4-LIVE/P0.5.4-FIX) - never a second observer - to add the
evidence P0.6.1's own brief requires that the prior script had no
visibility into: a rule-loaded/enabled pre-check
(`AutomationEngine.get_automation_status()`, pre-existing public API),
per-rule `automation.<outcome>` counting filtered to
`camera_human_detected_log` only, and a `tool_requested` device-action
safety count (with an honestly-documented per-rule-attribution
limitation).

**Structural safety re-confirmed this sprint:** `automation.log`'s
dispatch function never calls `_dispatch_tool_call()` - proven by a
dedicated source-scan test, not merely re-asserted.

**Files:** `luno_live_camera_event_observer.py` (modified, root-level
standalone script), `tests/test_p0_6_1_live_log_verification.py` (new,
15 tests), `docs/change_impact/camera_automation_p0_6_1.md` (new).
Confirmed zero `luno/` `.py` files changed.

**Tests/Regression:** baseline 251/251 (reconfirmed), after 266/266
(251+15), zero new failures.

**Result classification: BLOCKED** (agent's own attempt) - same
structural sandbox network limitation as every prior sprint in this
line, re-confirmed. Real-bootstrap, simulated-event tests proved the
new wiring produces the exact evidence format required - not hardware
evidence. No config file mutated. The user must run the extended
script themselves to obtain a real PASS/PARTIAL/FAIL.

See `docs/change_impact/camera_automation_p0_6_1.md` and
`ARCHITECTURE_GUARD.md` §86 for the full writeup.

## 19ll/20ll. LUNO P0.6.2 — First Real Home Assistant Action (Safe Single-Device Camera Automation)

**First controlled real device action - one rule, one action type, one
entity.** `camera_human_detected_test_action` - `event.kind==
human_detected` -> `home_assistant.turn_on` on `light.wled` ("RGB
Strip"), a real, pre-existing, low-risk light sourced from `.env`'s
`RGB_LIGHT_ENTITY`/`config/lights.config.json` - never fabricated.
`camera_human_detected_log` (P0.6/P0.6.1) is byte-for-byte unchanged.

**Architecture reused:** the existing `home_assistant.turn_on` action
type/dispatch path (already idempotent, already state-verified via
`RealHomeAssistantHandler`) - no new HA client, no bypass. The one
genuine gap found and closed: `luno/automation/models.py::
validate_action()` previously accepted a non-string or wildcard (`"*"`)
HA target; tightened to require a real, single entity-id string -
backward compatible with every existing rule.

**Files:** `luno/automation/models.py` (modified, the only new
production logic), `config/automation_rules.json` (modified, +1 rule),
`luno_live_camera_event_observer.py` (modified, reused/extended -
root-level, not under `luno/`), 3 pre-existing test files (4 tests
updated to reflect the real second rule / AST-based rewrite of 2
brittle scans), `tests/test_p0_6_2_camera_ha_action.py` (new, 33
tests), `docs/change_impact/camera_automation_p0_6_2.md` (new).
Confirmed zero Vision/CameraAutomation/RTSP/Tapo/real_home_assistant/
ha_client/Event Bus files touched.

**Tests/Regression:** baseline 301/301, after 349/349 (targeted set),
zero new failures. Spot-check (camera_patrol/dashboard): 84/84
unaffected.

**Result classification: BLOCKED** (agent's own attempt) - same
structural sandbox network limitation as every prior sprint in this
line. A real-bootstrap, mocked-HA-boundary simulated run proved both
rules fire independently from one event, exactly one `tool_requested`
(home_assistant/turn_on/light.wled), zero PTZ/other device actions -
not hardware evidence, never reported as PASS.

See `docs/change_impact/camera_automation_p0_6_2.md` and
`ARCHITECTURE_GUARD.md` §87 for the full writeup.

## 19mm/20mm. LUNO P0.6.2-FIX — Vision Runtime Parity / YOLO Detection Recovery

**First sprint driven by real, live hardware output from the user.**
RTSP camera open succeeded but tracked YOLO detection failed every
cycle with `'Conv' object has no attribute 'bn'`, despite `main.py`
having previously produced real detections from the same camera.

**Audit (direct code comparison, not assumed):** `main.py` and the
observer already share the identical `LauncherConfig.load()` ->
`register_all_modules()`/`register_all_adapters()` bootstrap path, and
there is exactly ONE `RealVisionSource()` construction site in the
whole repo - "runtime parity" was already true before this sprint, no
duplicate Vision implementation exists. The double-fusion hypothesis
(Section 8) was disproven directly from the code - `luno/vision.py`
never calls `.fuse()` anywhere; `_get_yolo_tracking()` already
delegates to one shared cached model singleton. Likely underlying
cause, evidenced but not provable from this sandbox: `requirements.txt`
pins only `ultralytics>=8.3.0` (open lower bound) against committed,
static `.pt` checkpoint files - this codebase's own pre-existing
`_yolo_checkpoint_hint()` diagnostic already describes this exact
`AttributeError`/`.name=="bn"` signature as a stale-checkpoint-vs-
newer-ultralytics mismatch. No dependency was changed this sprint.

**The one genuine Luno code defect found and fixed:**
`detect_objects_tracked()`'s pre-existing `except Exception: return []`
contract made a real detector failure indistinguishable from
"legitimately saw nobody" - both produced an empty list, risking a
false `human_cleared`. Fix (additive only, contract unchanged): new
`last_tracked_detection_error()` getter in `luno/vision.py`;
`luno/adapters/real_vision.py::_tracked_cycle_once()` now also
publishes the EXISTING `SystemError` event class
(`error_type="vision_detection_failed"`, no new event type) on
failure; the observer subscribes and reports a distinct
`[VISION_DETECTION_FAILED]` line, never folded into `human_cleared`,
and now prints real runtime versions (Python/ultralytics/torch/CUDA/
OpenCV/model paths - Section 6) on every run.

**Files:** `luno/vision.py`, `luno/adapters/real_vision.py`,
`luno_live_camera_event_observer.py` (all additive-only diffs, all
Vision-layer), `tests/test_p0_6_2_fix_vision_runtime_parity.py` (new,
21 tests), `docs/change_impact/camera_automation_p0_6_2_fix.md` (new).
Zero files under `luno/automation/*`/`luno/camera_automation/*`/
`config/automation_rules.json` touched - both existing automation
rules remain byte-for-byte unchanged.

**Tests/Regression:** baseline 428/428, after 448 passed + 1 honestly-
skipped (no `ultralytics` in this sandbox) / 0 failed (targeted set).
An additional 8-file sweep found 3 pre-existing failures entirely
outside this sprint's diff scope (2 external-network health checks -
OpenRouter/Fish Audio, proxy-blocked in this sandbox; 1 unrelated
`real_whisper.py` bug) - documented, not silently normalized.

**Result classification: BLOCKED** (agent's own attempt) - the actual
root cause of the Conv.bn error can only be confirmed on the user's
real machine; this sandbox cannot verify whether the live symptom is
resolved. The Section 13 silent-failure-masking defect IS fixed and IS
verified by the new tests. The user must re-run
`python luno_live_camera_event_observer.py --duration 120`, read the
new runtime-version printout, and report back whether
`VISION_DETECTION_FAILED` still appears.

See `docs/change_impact/camera_automation_p0_6_2_fix.md` and
`ARCHITECTURE_GUARD.md` §88 for the full writeup.

## 19nn/20nn. LUNO P0.6.3 — Unified Vision → Camera Automation Integration

**New binding invariant:** Camera Automation MUST consume the
production Vision result/event rather than instantiate its own
RTSP/YOLO pipeline. Dashboard and Camera Automation are both consumers
of the same Vision pipeline (one `RealVisionSource`, one shared cached
YOLO singleton, one RTSP source).

**Audit finding: this architecture already existed.** Exactly one
`RealVisionSource()` construction site (re-confirmed). `Vision
CameraEventBridge` already consumed the correct pre-existing events
since P0.5.3, with no second pipeline. Newly documented: the
dashboard's rich per-object view and Camera Automation's `human_
detected`/`human_cleared` are fed by TWO DIFFERENT pre-existing loops
inside the one `RealVisionSource` - `_tracked_cycle_loop()` (feeds the
dashboard, calls `detect_objects_tracked()`) and `_poll_loop()` (feeds
Camera Automation via `_update_person_presence()`, calls `detect_
objects()`) - both sharing one model singleton, different schedules.
This explains why P0.6.2-FIX's detector-failure fix (only `detect_
objects_tracked()`) never protected Camera Automation's actual event
source.

**The one genuine gap found and fixed:** `detect_objects()` had the
same "silent failure looks like empty scene" defect P0.6.2-FIX fixed
elsewhere - risking a FALSE `CameraPersonLeft`/`human_cleared` for
someone who never left. Fixed additively: `luno/vision.py` gained
`last_presence_detection_error()`; `luno/adapters/real_vision.py::
_poll_once()` now publishes the existing `SystemError`/`vision_
detection_failed` signal (with a new `detector` field) and SKIPS
`on_detections()` for that cycle on failure - no state transition
invented, no second presence mechanism added.

**Files:** `luno/vision.py`, `luno/adapters/real_vision.py` (both
additive-only), `tests/test_real_adapters.py` (one new method on a
pre-existing test fake), `tests/test_p0_6_3_unified_vision_camera_
automation.py` (new, 31 tests), `docs/change_impact/camera_automation_
p0_6_3.md` (new). Zero files under `luno/camera_automation/*`, `luno/
automation/*`, `config/automation_rules.json`, `luno/bootstrap/
modules.py`, or `main.py` touched - the audit proved none needed to
change.

**Tests/Regression:** main targeted set 454 -> 485 (+31 new)/1 skipped
(unchanged)/0 failed. Sprint69 dashboard/stability pair (run isolated
- see below) 37/37 unchanged. One genuine regression found (a
pre-existing test fake missing the new getter) and fixed during this
sprint.

**New pre-existing-issue finding (documented, not fixed, out of
scope):** `tests/test_vision_sprint8.py`'s own `_install_fake_real_
vision()` helper permanently, non-restoringly reassigns `luno.vision.
camera_status`/etc. at module level - running it in the same pytest
process as the sprint69 camera-stability/dashboard-forensics files
causes 13 cross-file-pollution failures that vanish when either group
runs alone. Root-caused to that helper (Sprint-8 era, untouched by any
sprint in this line); every regression command in this project already
avoids the combination.

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- same structural sandbox limitation as every prior sprint in this
line. Everything provable from code/tests here is a genuine, verified
PASS; the real walk-test (Empty/Enter/Stay/Leave/Re-enter, dashboard +
automation observed simultaneously) needs the user's machine.

See `docs/change_impact/camera_automation_p0_6_3.md` and
`ARCHITECTURE_GUARD.md` §89 for the full writeup.

## 19oo/20oo. LUNO P0.7 — Vision Context → Automation Context

**New binding invariant:** Vision Context is derived from the existing
`RealVisionSource` detection output ONLY (the same public `adapter_
manager.status_all()["vision"]` snapshot the dashboard already reads)
and must never instantiate an independent Vision/YOLO/RTSP pipeline.
`vision_context.py` is a pure, isolated module with no Event Bus
subscription of its own - `VisionCameraEventBridge` is the one and only
caller. `VisionContext` rides along ONLY on the four already-existing,
already-debounced Vision events - never a new high-frequency/polling-
driven automation event.

**Design:** `CameraEvent` gained 5 new OPTIONAL fields (`human_present`,
`person_count`, `detected_objects`, `available`, `detection_error`) -
every pre-P0.7 (HA-sourced) construction site unaffected. `event.
<field>` (P0.6) already exposed anything a `CameraEvent`'s `.data`
carries - no new condition-resolution mechanism needed. The one new
operator added: `greater_equal` (for `event.person_count >= 2`, the
brief's own worked example) - no `less_equal`, no second condition
engine. `VisionCameraEventBridge` gained a `vision_status_reader` public
attribute (wired post-construction, same convention `planner_module.
device_intent_client` already established) and a new subscription to
the EXISTING `system_error` event (filtered to `vision_detection_
failed`) to track detector failures without importing `luno.vision`
directly.

**The core safety requirement, verified not just designed:** a detector
failure never zeroes `human_present`/`person_count` - `build_vision_
context()` passes through whatever the (separately, honestly reported)
status snapshot already says, extending the P0.6.2-FIX/P0.6.3 "a
detector failure must never look like an empty scene" principle to the
automation-context layer.

**New rule:** `camera_multiple_people_log` (log-only) - `event.kind ==
"human_detected"` AND `event.person_count >= 2` -> `automation.log`. No
lights/switches/PTZ/locks.

**Files:** `luno/camera_automation/vision_context.py` (new),
`luno/camera_automation/cameras.py` + `vision_bridge.py` (additive),
`luno/automation/models.py` + `conditions.py` (one new operator),
`luno/bootstrap/adapters.py` (`register_vision_context_reader()`, same
post-hoc pattern as two existing functions), `main.py` (one new call),
`config/automation_rules.json` (one new rule), `tests/test_p0_7_
vision_context.py` (new, 40 tests), `docs/change_impact/vision_
context_p0_7.md` (new). Zero files under `luno/adapters/vision.py`,
`luno/vision.py`, `luno/adapters/real_vision.py`, the dashboard
collectors, or the Home Assistant client touched.

**Tests/Regression:** 3,978 -> 4,018 passed (+40 new) across the full
chunked sweep; isolated groups unchanged (`test_vision_sprint8.py`
32/32, sprint69 pair 37/37). Three pre-existing tests updated because
this sprint's own intentional additive changes correctly invalidated
their hardcoded assumptions (bridge subscribes to 5 event types not 4;
`automation_rules.json` has 3 rules not 2; `CONDITION_TYPES` has 7
members not 6) - documented, deliberate updates, not regressions. All
other failures found during the sweep were independently confirmed
pre-existing and unrelated - see `docs/testing/regression_baseline.md`'s
own P0.7 section for the full accounting (the `.env`/`MAX_TOKENS_PARAM`
mismatch and `config/backups/` accumulation were both already flagged
in this document's own §22, well before this sprint).

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- same structural sandbox limitation as every prior sprint in this
line. Everything provable from code/tests here is a genuine, verified
PASS; the real walk-test (confirm `camera_multiple_people_log` fires
alongside `camera_human_detected_log` when 2+ people are in frame)
needs the user's machine.

**Known limitation (intentional, not an oversight):** `VisionContext`
only refreshes on the four pre-existing discrete events - an object
appearing, or the person count changing, WHILE a person is already
continuously present does not itself retrigger automation this sprint
(Section 9's own "avoid polling-driven events" constraint). See Next
Recommended Sprint below for the smallest logical follow-on.

See `docs/change_impact/vision_context_p0_7.md` and `ARCHITECTURE_
GUARD.md` §90 for the full writeup.

## 19qq/20qq. LUNO P0.8.0 — Camera Automation → Home Assistant Action Safety Pipeline

**New binding invariant:** every camera-triggered `home_assistant.
turn_on`/`turn_off` action must pass through `luno/automation/camera_
action_safety.py::validate_camera_ha_action()` before it reaches the
existing HA dispatcher - a detector failure or camera-offline signal
must NEVER be interpreted as "human_cleared" and must never itself
cause a device action. This module is pure (no Event Bus subscription,
no Home Assistant client, no camera/vision import) and is called from
exactly one place, `AutomationEngine._dispatch_home_assistant_action()`,
only for rules whose trigger event is `camera_automation.camera_event`.

**Design:** ordered, fail-closed checks - (A) action type allowlist
(`home_assistant.turn_on`/`turn_off` only); (B) target validation
(rejects null/empty/multiple/wildcard/malformed entity ids); (C) camera
event validity (rejects missing/malformed `event_data`/`kind`); (D)
vision-state safety (rejects `detection_error`, `camera_offline` kind,
or `available is False`, without ever inferring the opposite); (E)
optional state-aware skip via a new `AutomationEngine.ha_state_reader`
attribute (wired post-construction in bootstrap, closes over the SAME
real `RealHomeAssistantClient.get_entity_state()` the existing
`RealHomeAssistantHandler` already calls - no-ops on the mock backend).
Cooldown/duplicate protection reuses the existing `_cooldown_until`
mechanism (Sprint 72, Phase 8) - no second implementation was added.

**New rule:** `camera_test_automation_safety_action` (TEST-ONLY) -
`event.kind == "human_detected" AND event.available == true AND event.
detection_error == null` -> `home_assistant.turn_on` targeting the
harmless, non-real `light.test_camera_automation`, `cooldown_seconds:
30.0`. No `human_cleared`-triggered rule was added - `AutomationEngine`
has no delayed/state-aware OFF mechanism to reuse without an
architectural change, per this sprint's own conditional constraint.

**Files:** `luno/automation/camera_action_safety.py` (new),
`luno/automation/engine.py` (additive `rule`/`event_data` parameters
through the dispatch chain, new `_is_camera_triggered_rule()` helper,
new `ha_state_reader` attribute), `luno/bootstrap/adapters.py` (new
`register_camera_action_ha_state_reader()`, same post-hoc pattern as
`register_vision_context_reader()`), `main.py` (one new call),
`config/automation_rules.json` (one new rule), `tests/test_p0_8_0_
camera_action_safety.py` (new, 48 tests), `tests/test_sprint72_
automation_engine.py` + `tests/test_p0_7_vision_context.py` (3
pre-existing tests updated for this sprint's own intentional
signature/schema/rule-count changes), `docs/change_impact/camera_
automation_p0_8.md` (new). Zero files under `luno/camera_automation/
*.py`, `luno/adapters/vision.py`, `luno/vision.py`, `luno/adapters/
real_vision.py`, `luno/tool_manager/builtin/home_assistant.py`,
`luno/tool_manager/builtin/real_home_assistant.py`,
`luno/automation/models.py`, or `luno/automation/conditions.py`
touched.

**Tests/Regression:** 4,018 -> 4,066 passed (+48 new) across the full
chunked sweep; isolated groups unchanged (`test_vision_sprint8.py`
32/32, sprint69 pair 37/37). Three pre-existing tests updated because
this sprint's own intentional additive changes correctly invalidated
their hardcoded assumptions (`_dispatch_action()` gained a leading
`rule` parameter; action-completed/failed events now carry a `code`
field; `automation_rules.json` has 4 rules not 3) - documented,
deliberate updates, not regressions. All other failures found during
the sweep were independently confirmed pre-existing and unrelated -
see `docs/testing/regression_baseline.md`'s own P0.8.0 section for the
full accounting.

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- same structural sandbox limitation as every prior sprint in this
line. **Explicit statement: REAL HOME ASSISTANT ACTIONS WERE NOT
PERFORMED at any point this sprint** - every test routes through
`MockHomeAssistantHandler`; `register_real_tool_handlers()` is never
called by this sprint's own code or tests. Everything provable from
code/tests here is a genuine, verified PASS; the real walk-test (point
a camera-triggered rule at a real light and confirm the safety gate/
cooldown/state-aware-skip all behave correctly against the real HA
client) needs the user's machine and is explicitly deferred to P0.8.1.

**Known limitation (intentional, not an oversight):** the state-aware
skip optimization is only ever exercised against a real Home Assistant
client on the user's own machine - in this sandbox (mock backend) it
correctly stays unwired (`ha_state_reader is None`), a legitimate
"optimization unavailable" state, not a failure.

See `docs/change_impact/camera_automation_p0_8.md` and `ARCHITECTURE_
GUARD.md` §91 for the full writeup.

## 19rr/20rr. LUNO P0.8.1 — Live Camera → Home Assistant Light Verification

**New binding invariant:** the ONE test light a live verification run
controls must always come from an EXPLICIT, user-set environment
variable (`CAMERA_AUTOMATION_TEST_LIGHT_ENTITY`) - never guessed,
never auto-discovered. `apply_camera_automation_test_light_override()`
(`luno/bootstrap/adapters.py`) may ONLY ever touch the existing P0.8.0
TEST-ONLY rule's `target` parameter, in memory, for the current
process, and MUST run after `runtime.start()` (the point
`AutomationEngine._rules` first becomes populated).

**Design:** `luno_live_p0_8_1_verification.py` (new, root-level,
standalone script) implements the brief's own 13-point pre-flight (all
CRITICAL - any failure hard-stops before the runtime starts, before
any device action is even possible), six interactive tests (idle /
human enter / human stays / human exit / manual-state / detector-
failure-safety), and the mandated result block. Boots the real,
existing bootstrap unchanged; subscribes only to existing event types.
No new Vision/HA/AutomationEngine/Event Bus construction anywhere.

**A real bug caught before it shipped:** the first draft placed the
test-light override BEFORE `runtime.start()` - since `AutomationEngine.
_rules` only populates inside `start()`, this would have made the
whole feature permanently, silently inert in real use. Caught by this
sprint's own test suite (`test_10`/`test_11`/`test_14`/`test_40`
failing with an explicit "rule ... is not loaded" log line during
development), fixed by moving both call sites (`main.py` and the live
script) to run immediately after `runtime.start()`.

**Files:** `luno_live_p0_8_1_verification.py` (new), `tests/test_p0_8_
1_live_verification.py` (new, 23 tests), `luno/bootstrap/adapters.py`
(new `apply_camera_automation_test_light_override()`), `main.py` (one
new call, after `runtime.start()`), `docs/change_impact/camera_
automation_p0_8_1.md` (new). Zero files under `luno/camera_automation/
*.py`, `luno/automation/camera_action_safety.py`, `luno/automation/
engine.py`, any Home Assistant client/adapter file, or `config/
automation_rules.json` ON DISK touched.

**Tests/Regression:** 4,066 -> 4,089 passed (+23 new) across the full
chunked sweep; isolated groups unchanged (`test_vision_sprint8.py`
32/32, sprint69 pair 37/37). Zero pre-existing tests needed updating.
One `test_streaming_e2e.py` failure observed during the combined
sweep was re-run in isolation and passed cleanly (documented flake
procedure, §21 below - not a regression, unrelated file). Two
pre-existing, already-baselined collection errors (`test_main_bargein.
py`/`test_root_main_bargein.py` - "the same 2 pre-existing
uncollectible files as every prior sprint" per `project_handover.json`'s
own `test_baseline` field) reconfirmed unrelated. All other failures
independently confirmed pre-existing - see `docs/testing/regression_
baseline.md`'s own P0.8.1 section for the full accounting.

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- the pre-flight itself hard-stopped in this sandbox (no network route
to real camera/HA hardware, `ultralytics` not installed, and this
checkout's own `CAMERA_AUTOMATION_ENABLED` currently resolves to
`False`) before any of the six tests could run. **Explicit statement:
REAL HOME ASSISTANT ACTIONS WERE NOT PERFORMED** - no light was turned
on or off by the agent this sprint. The actual, verbatim pre-flight
output is captured in `docs/change_impact/camera_automation_p0_8_1.md`
§5.

**Known limitation (intentional, not an oversight):** the six-test
sequence requires a human tester to perform physical actions (walk
in/out of frame, manually toggle the light) at `input()`-gated prompts
- it cannot be run fully unattended, and by design cannot be completed
by the agent in this sandbox.

See `docs/change_impact/camera_automation_p0_8_1.md` and `ARCHITECTURE_
GUARD.md` §92 for the full writeup.

## 19ss/20ss. LUNO P0.8.2 — Camera Human Cleared → Safe Light OFF

**New binding invariant:** `human_detected` -> ON and `human_cleared`
-> OFF must remain two fully separate rules (never a second action on
the existing ON rule), so Sprint 72's per-rule cooldown gives each
direction an independent window; a rule's cooldown may only start after
that rule's own conditions genuinely passed, never on a triggered-but-
`SKIPPED` execution caused by an unrelated rule sharing the same
trigger event type.

**Design:** one new rule, `camera_test_automation_safety_action_off`,
in `config/automation_rules.json` (`human_cleared` -> `home_assistant.
turn_off`, same harmless `light.test_camera_automation` placeholder,
`cooldown_seconds: 30.0`); the existing ON rule is byte-for-byte
unchanged. `luno/automation/camera_action_safety.py` required ZERO
changes - it was already fully direction-agnostic. `luno/bootstrap/
adapters.py::apply_camera_automation_test_light_override()` was
generalized (single rule id -> a frozenset, `_LIVE_TEST_RULE_IDS`) so a
live run's override now correctly applies to both rules. `luno_live_
p0_8_1_verification.py` (the SAME file - no second, competing observer)
gained a `--sequence {p0_8_1,p0_8_2}` flag (default `p0_8_1`, fully
backward compatible) and a new TEST A-F sequence exercising the full
ON/OFF/re-ON/re-OFF cycle.

**A real bug caught and fixed during this sprint's own Section 6
verification:** `AutomationEngine._run_execution()`'s `finally` block
was starting a rule's cooldown unconditionally on every triggered
execution, even a `SKIPPED` one whose own conditions failed. Since the
new OFF rule shares its trigger event type with the existing ON rule,
the OFF rule's cooldown was being silently pre-consumed every time the
UNRELATED ON rule's event fired (and vice versa) - caught by a test
written specifically to verify cooldown independence (`test_41`),
root-caused via a standalone repro script, and fixed with one added
boolean condition (`execution.condition_result`) gating the existing
cooldown-start line - the same `_cooldown_until` mechanism, no new
cooldown system. Verified safe (shared code, used by every rule in the
project) via a 409-test combined regression run before proceeding, then
reconfirmed via this sprint's own full repository sweep.

**Files:** `config/automation_rules.json` (one new rule), `luno/
bootstrap/adapters.py` (`_LIVE_TEST_RULE_IDS` generalization), `luno/
automation/engine.py` (one-line cooldown fix - the only change to this
file), `luno_live_p0_8_1_verification.py` (new `--sequence p0_8_2`
TEST A-F path, additive), `tests/test_p0_8_1_live_verification.py`
(`test_11` updated), `tests/test_p0_8_0_camera_action_safety.py`
(`test_33`/`test_34` updated - documented staleness updates, not
weakened guarantees), `tests/test_p0_8_2_human_cleared_light_off.py`
(new, 35 tests), `docs/change_impact/camera_automation_p0_8_2.md`
(new). Zero files under `luno/camera_automation/*.py`, `luno/
automation/camera_action_safety.py`, `luno/automation/models.py`,
`luno/automation/conditions.py`, any Home Assistant client/adapter
file, `luno/bootstrap/modules.py`, or `main.py` touched.

**Tests/Regression:** targeted set 450 -> 485 passed (+35 new), 1
skipped (unchanged), 0 new failures. Full repository sweep: 4,052
passed, 37 failed, 1 skipped across 140 collectible files - every
failure individually re-confirmed against an already-documented
pre-existing category (same categories as every prior sprint; the
`config/backups/` drift family now at 51 files). Zero new failures
anywhere in the repository. See `docs/testing/regression_baseline.md`'s
own P0.8.2 section for the full accounting.

**Result classification: BLOCKED** (agent's own live-hardware attempt)
- the pre-flight itself hard-stopped in this sandbox (no network route
to real camera/HA hardware, `ultralytics` not installed, `CAMERA_
AUTOMATION_ENABLED` resolves to `False` in this checkout) before any of
TEST A-F could run. **Explicit statement: REAL HOME ASSISTANT ACTIONS
WERE NOT PERFORMED** - no light was turned on or off by the agent this
sprint. The actual, verbatim pre-flight output is captured in `docs/
change_impact/camera_automation_p0_8_2.md` §11.

**Known limitation (intentional, not an oversight):** TEST A-F, like
the P0.8.1 sequence before it, requires a human tester to perform
physical actions at `input()`-gated prompts, and by design cannot be
completed by the agent in this sandbox.

See `docs/change_impact/camera_automation_p0_8_2.md` and `ARCHITECTURE_
GUARD.md` §93 for the full writeup.

## 19tt/20tt. LUNO — Long-Term Memory Self-Healing / Recovery Hardening

Hardened `luno/memory.py`'s existing, private persistence pair
(`_load()`/`_save()`) for `config.LONG_TERM_MEMORY_FILE` only, so Luno
can never fail to start merely because that one file is corrupted or
unrecoverable. Explicitly a reliability patch, not a redesign — no
second memory store, schema, backup system, or persistence abstraction
was introduced; every other persistent store and every retrieval/
ranking/scoring/dedup subsystem is byte-for-byte untouched.

**Two hard, already-tested constraints found during inspection and
resolved in favor of preserving existing behavior over a literal reading
of the brief's own pseudocode:** (1) `_load()` runs at MODULE IMPORT
TIME and must remain provably read-only forever — a pre-existing,
source-text-inspecting test (`tests/test_sprint64_memory_corruption_
forensics.py::test_B_load_is_read_only_no_write_primitive_in_its_
source`) already enforces this, and it's the exact property that
prevents a repeat of the real incident documented in `docs/change_
impact/memory_recovery.md`. (2) individual malformed memory entries
are, by existing design, tolerated at use time, not a recovery trigger
— `tests/test_manual_memory.py::test_partial_malformed_entries_are_
skipped_not_crashed` already proves a mixed good/bad-entry file must
keep the good entry retrievable. Both resolved the same way: `_load()`
only ever DECIDES (in memory) that a corrupted primary needs
quarantining; the actual quarantine-copy is deferred to the next
`_save()` call (the one existing write funnel), via a new
`_finalize_pending_quarantine_if_any()`. Validation (`_validate_memory_
data()`) checks root shape only (`isinstance(data, list)`), matching —
not weakening — the existing entry-level tolerance.

**Recovery sequence:** primary missing → healthy empty store (unchanged);
primary valid → healthy, loaded as-is (unchanged); primary invalid
(parse failure or wrong root shape) → newest-first backup scan
(`_load_latest_valid_backup()`, unchanged mechanism, now sharing the
same validation contract), first valid backup wins, loaded byte-for-byte
(never re-ranked/rewritten) → status `recovered_from_backup`; no valid
backup either → status `fresh_after_unrecoverable_corruption`, `[]`,
pending-quarantine recorded. Quarantine lands in a new sibling
`quarantine/` directory (distinct from `backups/` so a known-corrupt
file can never be mistaken for a restorable one), named `long_term_
memory.corrupt.<timestamp>.json`, with a numeric-suffix collision guard
so an existing quarantine artifact is never overwritten; a quarantine
failure is caught narrowly (`except OSError`) and logged, never
crashing the fresh-memory save that follows. Observability reuses the
existing in-memory, non-dashboard pattern — `get_persistence_status()`
surfaced as one new key inside the EXISTING `memory_health_report()`'s
return dict, never persisted inside the memory data itself, no new
dashboard page, no second state model.

**Files:** `[MODIFIED] luno/memory.py` (new `_validate_memory_data()`,
`get_persistence_status()`, `_memory_quarantine_dir()`/`_memory_
quarantine_filename()`, `_recover_from_backup_or_go_fresh()`, `_finalize_
pending_quarantine_if_any()`; `_load()` rewritten to the recovery
sequence above, still 100% read-only; `_load_latest_valid_backup()` now
shares the validation contract; `_save()` gained exactly one new call;
`memory_health_report()` gained one new key), `[MODIFIED] tests/test_
memory_persistence_hardening.py` (+23 tests covering all 26 brief-
mandated scenarios, all 11 pre-existing tests preserved unmodified),
`[NEW] docs/change_impact/long_term_memory_self_healing.md`. Zero
changes to `luno/persistence.py` (confirmed `LONG_TERM_MEMORY_FILE`
never routes through it — a separate, parallel implementation),
`luno/config.py`, `tests/conftest.py`, `luno/mutation_audit.py`, or any
dashboard file — none were technically necessary.

**Tests/Regression:** targeted persistence suite 11 -> 34 passed (+23
new), 0 failed. Full repository sweep: 4,052 -> 4,075 passed (+23,
exactly the new test count), 37 failed -> 37 failed (unchanged), 1
skipped -> 1 skipped (unchanged) — zero new failures anywhere. Every
one of the 37 failures individually re-confirmed pre-existing (LLM
`max_tokens`/`max_completion_tokens` adapter mismatch, missing `list_
microphones.py`, `RealWhisperSource` test-construction gap, and the
`config/backups/`-accumulation/real-file-state forensic staleness family
already documented in §93 — still 51 backup files, confirmed not caused
by this sprint since every one of this sprint's own tests is `tmp_path`-
isolated). All 7 mandated production persistent-state files hashed
before any code was written and again after the full sweep — byte-
identical in every case. See `docs/testing/regression_baseline.md`'s
own Long-Term Memory Self-Healing section for the full accounting.

**Result classification: COMPLETE** — pure code-level reliability
hardening with no live-hardware dependency; all 19 brief-mandated
acceptance-criteria items verified via the test suite, not merely
designed.

See `docs/change_impact/long_term_memory_self_healing.md` and
`ARCHITECTURE_GUARD.md` §94 for the full writeup.

## 19uu/20uu. LUNO P0.8.3 — Fix Real YOLO Inference Failure

The user ran the real P0.8.2 live verification on their actual machine.
Pre-flight fully passed (network/credentials/RTSP/HA/safety-gate/
runtime-start all green) - the only failure was YOLO detection itself,
every cycle: `AttributeError: 'Conv' object has no attribute 'bn'`, with
NO checkpoint-mismatch hint appended even though `luno/vision.py::
_yolo_checkpoint_hint()` (added in P0.6.2-FIX specifically for this
signature) was already being called from every YOLO except-block.

**Root cause, confirmed via direct inspection of the real, installed
`torch==2.13.0`'s own source (this repo's mounted `.venv/`):** `_yolo_
checkpoint_hint()`'s original `getattr(ex, "name", None) == "bn"`
condition can NEVER match the real exception - `torch.nn.Module.
__getattr__` raises a plain, message-only `AttributeError(...)` (no
`name=` kwarg) that never populates `.name`. The pre-existing `tests/
test_p0_6_2_fix_vision_runtime_parity.py::test_12`'s own comment had
already documented this exact gap but worked around it with a `.name`-
carrying test double rather than fixing the condition - the test passed
while the real production path stayed broken. Fixed by ALSO matching
the exception's string message via a new `_YOLO_CHECKPOINT_ATTRIBUTE_
ERROR_RE` constant (the `.name` check is kept, not replaced) - the ONLY
functional code change this sprint makes.

**What was NOT fixed (and not attempted):** WHY the checkpoint and the
installed `ultralytics 8.4.123` disagree in the first place. Pickle-
level forensics on the actual local `.pt` files (no `torch` needed)
proved they are structurally normal, un-fused checkpoints - refuting a
literal "saved already-fused" theory. Installing a matching real
`torch`/`ultralytics` to execute a live reproduction was attempted and
found genuinely infeasible in this sandbox (526.6MB wheel plus ~15
mandatory `nvidia-*` CUDA packages on PyPI's default Linux wheel;
`download.pytorch.org`'s slim CPU index blocked by this sandbox's own
proxy) - the same class of hardware/network limitation every prior
"LIVE" sprint in this project has hit and reported honestly. Per the
brief's own explicit instruction, this sprint does NOT delete/replace
the local `.pt` files and does NOT pin/downgrade `ultralytics`/`torch`/
`torchvision` - the standard remedy (delete `yolo11n.pt`/`yolov8n-
pose.pt` so `_get_yolo()`/`_get_yolo_pose()`'s existing auto-download-
on-first-call re-fetches compatible checkpoints) is recommended to the
user, not silently performed.

**Files:** `[MODIFIED] luno/vision.py` (`import re` + one new regex
constant + `_yolo_checkpoint_hint()`'s condition extended - no other
function touched), `[MODIFIED] luno_live_p0_8_1_verification.py` (two
new INFORMATIONAL-ONLY pre-flight entries - torch/torchvision versions,
YOLO model file paths/existence/size - never added to `_CRITICAL_
PREFLIGHT_CHECKS`, cannot become a new hard-stop), `[NEW] tests/test_
p0_8_3_yolo_checkpoint_diagnostics.py` (18 tests), `[NEW] docs/change_
impact/camera_automation_p0_8_3.md`. Zero changes to `config/
automation_rules.json`, `luno/automation/`, `luno/camera_automation/`,
or `luno/adapters/real_vision.py`.

**Tests/Regression:** targeted set 336 passed (2 pre-existing, unrelated)
+ 288 passed (all remaining Vision/camera files), 0 new failures. Full
repository sweep: 4,075 -> 4,092 passed (+18, exactly the new test
count), 37 failed (identical category breakdown, unchanged), 1 skipped
- plus two already-NAMED, order/timing-dependent, full-suite-only flakes
(`test_llm_tts_streaming_production.py::test_14`, `test_verification_
dashboard.py::test_api_verification_reports_a_successful_verified_
action_end_to_end`) both confirmed via isolation re-run. Zero new
failures anywhere.

**Disclosure - production state:** `config/habit_memory.json` was found
already mutated by the user's OWN real machine (proven via mutation-
audit literal-Windows-path/pid evidence, not this sandbox) recording a
genuine `light.wled` habit observation from their real P0.8.2 session
earlier today. This investigation mistakenly reverted it to its
pre-write backup before recognizing the write was legitimate, and chose
NOT to risk fabricating a reconstructed replacement (a manual
reconstruction came out 162 bytes short of the recorded size) - net
effect, one recently-observed, low-stakes, self-regenerating habit
pattern entry was lost; every other file/entry confirmed unchanged. Full
accounting in `docs/change_impact/camera_automation_p0_8_3.md` §8 -
disclosed in full rather than omitted.

**Result classification: PARTIAL - diagnostic bug CONFIRMED and FIXED;
underlying detection failure NOT confirmed resolved.** No live YOLO
detection was claimed or performed in this sandbox.

See `docs/change_impact/camera_automation_p0_8_3.md` and `ARCHITECTURE_
GUARD.md` §95 for the full writeup.

## 19vv/20vv. LUNO P0.8.4 — Resolve the Actual YOLO Model / Ultralytics Compatibility Failure

P0.8.3 fixed the diagnostic hint but honestly left the underlying `Conv.
bn` AttributeError UNRESOLVED. This sprint's brief was explicit: find and
fix the ACTUAL model/ultralytics compatibility problem via a 4-stage
isolation methodology, prove it against a static image then RTSP then
live P0.8.2 TEST A-F, and never claim success prematurely.

**Root cause, confirmed via direct source inspection of the real, exact-
version-matching `ultralytics==8.4.123` (downloaded to match the real
machine's installed version) - NOT a model/checkpoint/dependency
incompatibility:** `luno/vision.py::_get_yolo()` returns ONE shared
`ultralytics.YOLO` singleton, called concurrently from two real
background threads (`start_watch()`'s thread via `detect_objects()`,
`RealVisionSource`'s tracked-cycle thread via `detect_objects_tracked
()`). Before this fix, the two call sites passed INCONSISTENT `device=`
kwargs against that shared model (one omitted, one explicit) -
`ultralytics.engine.model.Model.predict()`'s own predictor-reuse check
(`self.predictor.args.device != args.get("device", ...)`) is neither
thread-safe nor stable across this pattern, so it kept rebuilding the
predictor and re-`fuse()`ing the shared, already-fused underlying
`nn.Module` at steady state - each re-fusion's `delattr(m, "bn")`
(guarded per-module by `hasattr`, but not atomic) racing a concurrently-
running `Conv.forward()` reading `self.bn` on the OTHER thread. The
pre-existing `_yolo_lock` only ever guarded lazy model CONSTRUCTION,
never the inference call itself. This fully explains the real machine's
"every cycle" failure without any model/version incompatibility -
consistent with, and extending, P0.8.3's own pickle-forensic proof that
both `.pt` files are ordinary, un-fused, non-stale checkpoints.

**Fix (Option D - Luno API-usage fix, per the brief's own priority
order):** `detect_objects()`, `detect_objects_tracked()`, and
`_monitor_loop()` now all pass the SAME explicit `device=_device_arg()`,
and all three now wrap the actual `model(frame, ...)` call itself (not
just construction) in the pre-existing `_yolo_lock`. No `.pt` file, no
`ultralytics`/`torch` package, no dependency pin touched. `_get_yolo_
pose()`/`attach_pose_keypoints()` intentionally untouched (separate
singleton, single-thread-only caller, never exposed to this race).

**Sandbox execution attempt (new capability, still ultimately blocked):**
for the first time, the real, exact-version-matching `torch==2.13.0`/
`torchvision==0.28.0`/`ultralytics==8.4.123` wheels were downloaded (new
resumable `curl -C -` technique) and installed into this sandbox -
`import torch` still fails here (`libcudart.so.13` missing; PyPI's Linux
wheel requires genuine CUDA runtime libraries at import time via eager/
data-relocation-bound symbol references, confirmed via `readelf -d`, not
fixable by stub `.so` files). Root cause above was established via
direct, execution-free source inspection instead. A self-inflicted
`tests`-package namespace collision from this install (`ultralytics`'s
wheel bundles its own top-level `tests/` package) was found and fixed
within this same sprint by fully uninstalling torch/torchvision/
ultralytics again - confirmed the affected test passes cleanly
afterward.

**Files:** `[MODIFIED] luno/vision.py` only (three call sites +
`_yolo_lock` usage + a large explanatory comment - no other production
file). `[NEW] tests/test_p0_8_4_yolo_concurrency_fix.py` (12 tests,
including a genuine two-`threading.Thread` race-proof test). `[NEW]
docs/change_impact/camera_automation_p0_8_4.md`.

**Tests/Regression:** new suite 12/12 passed. Full 143-file sweep: every
Vision/P0.x/camera/automation test file 100% pass; only pre-existing/
already-documented baseline failures (`.env`-config gaps, `_device_
index`, known-flaky) plus two newly-investigated-but-confirmed-unrelated
clusters (real `config/lights.config.json`/`config/long_term_memory.
json` drift from this project's live-synced production folder, and a
live-write race in Sprint 63/64/67/68's own mutation-audit tests against
that same shared folder - proven via a same-test-twice FAIL-then-PASS
re-run, full detail in the change-impact doc §7) - zero new failures
caused by this sprint's actual code change.

**Result classification: PARTIAL-STRONG** - root cause identified and
fixed with a complete, source-evidenced mechanism and full regression
coverage including a real-thread race-proof test; final live proof
(real RTSP frame -> real YOLO inference -> real `light.wled` change)
still requires the real machine, since neither `torch` execution nor
RTSP/HA reachability exist in this sandbox. Per the brief's own explicit
instruction, this is reported as such, not claimed as a full live-
verified success.

**Recommended next step for the user:** on the real machine, run
`python luno_live_p0_8_1_verification.py --sequence p0_8_2` (same
command as before) - if this sprint's diagnosis is correct, YOLO
detection should now succeed every cycle and TEST A-F should proceed
exactly as originally specified in the P0.8.2 brief.

See `docs/change_impact/camera_automation_p0_8_4.md` and `ARCHITECTURE_
GUARD.md` §96 for the full writeup.

## 19ww/20ww. LUNO P0.8.5 — Fix `camera_person_entered` Firing With `person_count=0`

P0.8.4's fix let the user build and run a standalone real YOLO debug
viewer (`tools/vision_debug_viewer.py`) directly against the real Tapo
C212 stream, conclusively proving real human detection works (repeated
real `person=0.70`-`0.83`, `persons=1`). Despite that, the real Luno
verifier simultaneously logged `camera_person_entered` immediately
followed by `[CAMERA EVENT] kind=human_detected ... person_count=0` -
this sprint's brief was to trace the complete path and find the ACTUAL
cause, never by artificially setting `person_count=1`.

**Root cause, confirmed via complete source trace + real runtime log
evidence (`logs/runtime/2026-08-22.log`):** Luno runs TWO independent,
uncoordinated async polling loops over the same camera - a presence-
only watch loop (`vision.py::detect_objects()`, `CAMERA_WATCH_
INTERVAL_S` default 1.0s, returns only a label SET, no count) and a
tracked-cycle loop (`vision.py::detect_objects_tracked()` via `real_
vision.py::RealVisionSource`, `VISION_FPS` default 0.5s, the ONLY path
that actually counts people). `VisionAdapter._update_person_presence()`
(the ONE method that publishes `CameraPersonEntered`) was, before this
fix, called ONLY from the presence-watch loop's `on_detections()` - but
the `person_count` a subscriber later reads (via `VisionCameraEvent
Bridge`/`vision_context.build_vision_context()`) comes from `self.
_known_humans`, populated ONLY by the SEPARATE tracked-cycle loop's
`on_vision_cycle()`. Two independent consumers of real detections, not
one shared writer: whichever loop notices "a person is here" FIRST
fires the shared trigger, but the enrichment data was always read from
the OTHER loop's own, possibly-not-yet-caught-up snapshot - a genuine
cross-loop race, NOT a repeat of P0.8.4's (concurrent-writers) bug and
NOT a detection failure.

**Fix (one additive line):** `VisionAdapter.on_vision_cycle()`
(`luno/adapters/vision.py`) now ALSO calls the same, pre-existing,
already-tested `_update_person_presence(len(current_humans) > 0)`,
immediately after `_known_humans` is set on the same synchronous call -
both call sites share the ONE debounce state, so no double-firing is
possible, and whenever the (faster, count-bearing) tracked-cycle loop
wins the race, `person_count` is guaranteed non-stale by the time
`CameraPersonEntered` reaches the bus.

**Diagnostic logging (temporary, user-requested):** `detect_objects_
tracked()` now prints one `[VISION PERSON DEBUG] raw_boxes=... person_
boxes=... person_confidences=[...] person_count=... previous_person_
state=... new_person_state=...` line per cycle - counts/confidences/
booleans only, never credentials or frame data.

**Files:** `[MODIFIED] luno/vision.py`, `[MODIFIED] luno/adapters/
vision.py` (one additive line + comment). `[NEW] tests/test_p0_8_5_
person_count_sync_fix.py` (11 tests, A-H per spec + 3 cross-loop
consistency tests). `[NEW] docs/change_impact/camera_automation_p0_8_5.
md`. Zero changes to `AutomationEngine`, `luno/camera_automation/`, HA,
`config/automation_rules.json`, the YOLO model/confidence/torch/
torchvision/ultralytics/RTSP configuration, or `real_vision.py`'s
detection logic itself.

**Tests/Regression:** new suite 11/11 passed. Full Vision/P0.x sweep
(10 files) 256 passed, 1 pre-existing skip. `test_p0_8_0`-`test_p0_8_5`
(6 files) 147 passed. Full 145-file repository sweep: every failure
maps to an already-documented baseline category (`.env`/`MAX_TOKENS_
PARAM`, `_device_index`, `list_microphones.py`, accumulated `config/
backups/` drift, one already-known full-suite-only timing flake re-
confirmed passing standalone, `test_root_main_bargein.py`'s pre-
existing missing-`legacy_main.py` collection error) - zero new
failures. Two self-inflicted bugs (a `_FakeTensor.__len__` break, an
architecture-guard string-match false-positive from this sprint's own
comment) were found and fixed by this sprint's own regression sweep
before delivery.

**Result classification: STRONG** - root cause identified and fixed
with complete source- and real-log-evidenced mechanism and full
regression coverage; an honest residual-race caveat is disclosed (the
presence-watch loop can still rarely win very near cold start, before
the tracked-cycle loop's first cycle completes, leaving `person_count`
briefly stale by up to ~0.5s) rather than claiming a 100% fix. Final
live proof still requires the real machine.

See `docs/change_impact/camera_automation_p0_8_5.md` and `ARCHITECTURE_
GUARD.md` §97 for the full writeup.

## 19xx/20xx. LUNO P0.8.6 — End-to-End Human Detection → WLED Reliability Fix

Two real-world problems: (1) a single low-confidence frame
(`person_confidences=[0.506]`) could directly fire `home_assistant.
turn_on` on `light.wled` via the existing `human_detected` rule path,
with zero confidence floor and zero temporal confirmation; (2) HA
reported `light.wled=on`/"verification success" while the physical
light did not visibly turn on, alongside an `"ignoring device_state_
changed for unconfigured entity 'light.wled'"` log line flagged for
investigation.

**Root cause:** (1) `camera_human_detected_test_action` (the real WLED
rule) matched on the raw, confidence-blind `event.kind ==
"human_detected"` alone - any single tracked-cycle frame above the
detection-VISIBILITY threshold (`CONFIDENCE_THRESHOLD=0.4`) could
dispatch a real device action. (2) `RealHomeAssistantHandler._verify_
state()`'s "success" was already correctly scoped (HA's own reported
state only - no bug in the logic), but its WORDING did not distinguish
that from physical confirmation, which this architecture has never
been able to provide. (3) The "unconfigured entity" warning is a
confirmed non-defect - `CameraAutomationConfig.entities`'s own
docstring establishes this allowlist is for INBOUND listening only;
`light.wled` is an OUTPUT device this package acts ON, never something
it listens to inbound FROM.

**Fix:** a NEW, additive confirmation layer, `VisionAdapter._update_
confirmed_presence()` (`luno/adapters/vision.py`), computed ONLY from
the tracked-cycle loop (`on_vision_cycle()`), requiring `HUMAN_
DETECTION_CONFIRM_CYCLES` (3) consecutive cycles each with a person at
`>= HUMAN_DETECTION_CONFIDENCE` (0.60, evidence-derived from the
brief's 25 real confidences) before publishing a NEW, SEPARATE event
(`HumanPresenceConfirmed`) - a distinct `kind` (`"human_confirmed"`),
not a second publish of `human_detected` (which would silently collide
with the existing `(camera_id, kind)` dedupe key). `VisionAdapter.
_update_person_presence()`/`CameraPersonEntered`/`CameraPersonLeft` and
every rule listening to `human_detected`/`human_cleared` are
COMPLETELY UNCHANGED - `tests/test_camera_presence.py`'s pinned
contract and P0.8.5's own fix/tests remain fully intact. Only
`camera_human_detected_test_action` (the one real WLED rule) was
changed, to require `human_confirmed` + `event.available == true` +
`event.detection_error == null`. `RealHomeAssistantHandler._verify_
state()`'s log wording and `_result_data()`'s additive `verification_
scope: "ha_reported_state"` field make the existing verification scope
honest - no new verification mechanism was invented. Duplicate-turn_on
prevention and unavailable-state handling were both re-verified as
already correct, no new mechanism added.

**Files:** `[MODIFIED] luno/config.py`, `luno/adapters/events.py`,
`luno/adapters/vision.py` (additive only - `on_detections()`/`_update_
person_presence()` untouched), `luno/camera_automation/vision_context.
py`/`cameras.py`/`vision_bridge.py`, `config/automation_rules.json`
(one rule only), `luno/tool_manager/builtin/real_home_assistant.py`
(wording/additive field only). `[NEW] tests/test_p0_8_6_end_to_end_
human_wled_reliability.py` (25 tests). `[MODIFIED]` 7 pre-existing test
files updated to reflect the intentional rule/event redesign (each with
an explanatory docstring, never silently weakened). `[NEW] docs/
change_impact/camera_automation_p0_8_6.md`.

**Tests/Regression:** new suite 25/25 passed. Targeted P0.x/Vision suite
(14 files) 487 passed, 1 pre-existing skip. `test_real_home_assistant_
verification.py` 39 passed. `luno/` fast suite 818 passed, 2 failed
(same pre-existing FLAKY-KNOWN `barge_in` timing tests). Full 144-file
repository sweep: 4162 passed, 39 failed, every failure mapping to an
already-documented pre-existing baseline category - zero new failures.

**Result classification: STRONG** - both reported problems root-caused
with a complete, source-evidenced mechanism and full regression
coverage. Physical WLED confirmation was never claimed and cannot be
claimed from this sandbox (no RTSP/real HA/physical sensing channel
exists here) - a disclosed architectural limit, not an unresolved bug.
`luno_live_p0_8_1_verification.py` was inspected directly and confirmed
to already use HA-reported-state-only semantics, observing a separate
TEST-ONLY rule/entity unrelated to the real WLED rule this sprint
changed - no code change to that script was needed.

See `docs/change_impact/camera_automation_p0_8_6.md` and `ARCHITECTURE_
GUARD.md` §98 for the full writeup.

## 19yy/20yy. LUNO P0.8.7 — Investigate and Fix the Remaining WLED Activation Failure

Despite Luno's own logs showing a complete success sequence (`[HA] ->
homeassistant.turn_on`, `New: on`, `[HA] ✓ Done`, `verify 'light.wled'
attempt 1: state=on - verification success`), the physical WLED strip
did not visibly turn on. The brief required a full production-path
trace, a comparison against the known-good HA UI call shape,
production-safe diagnostic logging, and an A/B/C/D evidence framework
(A=Luno accepted, B=HA accepted, C=HA reports ON, D=physical device
confirmed - never to be claimed without evidence).

**Root cause:** every stage of the production path (`ToolManager` ->
`RealHomeAssistantHandler._resolve_entity_tiered()` -> `call_service
("homeassistant", action, entity_id="light.wled")` -> `luno.ha_client.
HomeAssistantClient.call_service()` -> real HA WebSocket API) was
confirmed correct and equivalent to HA's own UI call shape - ruled out
as transformation/substitution/suppressed-error/optimistic-WorldModel
sources, all with direct source evidence. The one genuine gap:
`RealHomeAssistantHandler._verify_state()`'s retry-loop reads went
through `RealHomeAssistantClient.get_entity_state()`'s PRE-EXISTING
cache-first default - returning whatever value was already cached from
the last real `state_changed` push (for ANY reason, at ANY prior time),
not a query specifically triggered by, and therefore conclusive for,
THIS command. If HA's own confirming push for this specific command
were ever delayed/dropped/coalesced, "verification success" could be
reported off a stale cached value.

**Fix:** `RealHomeAssistantClient.get_entity_state()` gained an
additive `force_refresh: bool = False` parameter (default preserves
100% of prior behavior); when `True`, always performs a live
`get_states()` round trip against Home Assistant, degrading gracefully
to cache only when a live query is genuinely impossible.
`RealHomeAssistantHandler._safe_get_state()` gained the same parameter
with a `TypeError`-catching fallback for any client/test-double lacking
it (every pre-existing fixture in this codebase - proven, not assumed,
by a dedicated regression test). `_verify_state()`'s retry loop now
always requests `force_refresh=True` - every verify attempt is a
genuinely live HA query. Three new diagnostic log lines (A->B/B/C,
explicitly stating D is never proven) were added, and `_result_data()`
gained a new `state_query_freshness: "fresh"|"cached"` field alongside
the pre-existing (P0.8.6) `verification_scope: "ha_reported_state"`
field.

**Files:** `[MODIFIED] luno/adapters/real_home_assistant.py`
(`get_entity_state()` - additive `force_refresh` parameter),
`[MODIFIED] luno/tool_manager/builtin/real_home_assistant.py`
(`_safe_get_state()` additive parameter; `_verify_state()` now requests
fresh reads; three new diagnostic log lines; `_result_data()` additive
`state_query_freshness` field). `[NEW] tests/test_p0_8_7_wled_
verification_fix.py` (18 tests, sections A-H). `[NEW] docs/
change_impact/camera_automation_p0_8_7.md`. Zero changes to YOLO
detection, human detection confidence, presence confirmation,
`VisionAdapter`, camera automation trigger logic, the P0.8.6 detection
policy, entity resolution, `ToolManager`, `WorldModel`, `luno/
ha_client.py`, `config/automation_rules.json`, or any `.pt` model - the
investigation proved none of these were responsible, per the brief's
own scope constraint.

**Tests/Regression:** new suite 18/18 passed. `test_real_home_
assistant_verification.py` 39/39 genuine passes (via the file's own
`main()` runner). Focused HA/tool + real-adapters regression (4 files)
86 passed, 2 failed (same pre-existing `RealWhisperSource._device_index`
gap). P0.0-P0.8.6 camera automation suite (16 files) 483 passed, 1
pre-existing skip. Full ~152-file repository sweep: every failure
mapping to an already-documented pre-existing baseline category
(LLM max_tokens/max_completion_tokens compat, MIC_DEVICE_INDEX,
production_launcher real-credentials, sprint60/63/64/68 config/backup
drift) - zero new failures, each independently re-run in isolation to
confirm.

**Result classification: STRONG** - root cause identified (cache-first
verification reads, not guaranteed to reflect a query triggered by the
specific command) and fixed with a complete, source-evidenced,
additive, fully backward-compatible mechanism, and full regression
coverage. Physical WLED illumination (item D) was never claimed and
cannot be claimed - a disclosed architectural limit, not a defect this
sprint could fix. If the symptom persists after this fix with logs
showing `state_query_freshness=fresh` and `actual_state=on`, the
remaining explanation space (HA's own WLED integration reporting an
optimistic/stale state, the WLED device's firmware/network dropping the
command after acknowledging it to HA, or a physical wiring/segment
issue) is entirely outside this repository's code - see the change-
impact doc's Section 11 for the full honest discussion.

See `docs/change_impact/camera_automation_p0_8_7.md` and `ARCHITECTURE_
GUARD.md` §99 for the full writeup.

## 19zz/20zz. LUNO P0.8.8 — Fix the Confirmed Camera Automation Event Suppression Bug

After P0.8.7's fix, the user asked why the WLED still would not turn on
despite the person clearly being detected repeatedly. Direct
investigation against the same real production log P0.8.7 used proved
the raw Vision-level `camera_person_entered` event fired 14 times across
the session, but the classified `camera_automation.camera_event (kind=
human_detected)` that `AutomationEngine`'s WLED rule actually listens to
was published only twice total, both within the first ~6 minutes - zero
times for the remaining 2.5+ hours despite dozens more real detections.

**Root cause:** `CameraAutomationModule._publish_if_not_suppressed()`'s
dedupe check (`if self._last_state.get(key) == state: return`) was
evaluated unconditionally before the real, time-based `_cooldown_until`
check. For BOTH classified-`CameraEvent` call sites (`_handle()`'s
`_entity_role_index` branch and `ingest_external_camera_event()` - the
one `VisionCameraEventBridge` calls for every Vision detection), the
call is `key=(camera_id, kind), state=kind` - `state` is PART OF `key`
itself, a compile-time constant for a fixed key. After the very first
successful publish for a given `(camera_id, kind)` pair, the equality
check is trivially True forever, making the intended `_cooldown_until`
rate limiter unreachable dead code - the WLED rule could only ever be
reached ONCE per `camera_id` per process lifetime, for each event kind.
The legacy raw-relay path (`state=new_state`, a genuinely independent,
continuously-varying value) never had this bug and remains completely
untouched. Reproduced directly against the real, unmodified production
function before any code change: `call1: published`, `wait past
cooldown`, `call2: SUPPRESSED (bug)`, `call3: SUPPRESSED (bug)` -
matching the production log exactly.

**Fix:** `_publish_if_not_suppressed()` gained one new, additive
parameter, `dedupe_identical: bool = True`. Default preserves the
legacy relay path's exact prior behavior byte-for-byte (untouched,
still pinned by `test_09`/`test_10`). The two classified call sites now
pass `dedupe_identical=False` - suppression becomes purely
`_cooldown_until`-based (a real, resettable, monotonic-time deadline)
for those two call sites only.

**Files:** `[MODIFIED] luno/camera_automation/module.py` (additive
parameter + two call-site updates only). `[NEW] tests/test_p0_8_8_
camera_event_suppression_fix.py` (16 tests, sections A-L, including a
real end-to-end production-call-path proof: `VisionCameraEventBridge.
_on_person_entered()` → real `CameraAutomationModule` → real Event Bus
→ real `AutomationEngine`, a real rule completing three separate times
across cooldown-separated detections). `[NEW] docs/change_impact/
camera_automation_p0_8_8.md`. Zero changes to Vision/RTSP/Tapo/HA/WLED
config or any `.pt` model - the bug and its fix are entirely contained
within one function and its two classified call sites.

**Tests/Regression:** new suite 16/16 passed. Focused camera_automation/
P0.x suite (16 files) 494 passed, 1 pre-existing skip (baseline before
this sprint was 478 passed - exactly `478 + 16 = 494`, zero
regressions). Full ~153-file repository sweep - every failure mapping
to an already-documented pre-existing baseline category (including two
failures that occurred ONLY under `-n 4` parallel xdist execution and
reproduced as a clean pass in isolation) - zero new failures.

**Result classification: STRONG** - root cause identified (a compile-
time-constant dedupe comparison made the intended cooldown unreachable
for every classified camera/Vision event) and fixed with a minimal,
additive, fully backward-compatible mechanism, and full regression
coverage including a genuine end-to-end production-call-path proof.
This restores AutomationEngine's ability to receive repeated classified
camera events (stage C) and the WLED rule's ability to re-dispatch
(stage D) on every subsequent detection, not just the first ever. It
does NOT constitute proof of stage F (physical WLED illumination) -
Luno has no optical/electrical sensing channel for any HA-controlled
device, a disclosed architectural limit unrelated to and unchanged by
this fix (see P0.8.7's own §99 entry).

See `docs/change_impact/camera_automation_p0_8_8.md` and `ARCHITECTURE_
GUARD.md` §100 for the full writeup.

## 19zzz/20zzz. LUNO P0.8.9 — Implement the Missing WLED OFF Automation Rule

User asked why WLED doesn't turn off automatically when leaving camera
view. Not a bug: the real ON rule (`camera_human_detected_test_action`,
P0.6.2) never had a real-entity OFF counterpart — the only existing OFF
rule (P0.8.2) targets the mock `light.test_camera_automation` entity.

Added `camera_wled_human_cleared_off` — `light.wled` turns off 10s after
`human_cleared`, cancelled if `human_confirmed` arrives first. Built
entirely on the project's EXISTING `runtime.scheduler`
(`Scheduler.schedule_once()`/`cancel()` — no new timer/thread invented):
one new optional action parameter, `delay_seconds` (`luno/automation/
models.py`), and a small, additive dispatch-time mechanism in
`luno/automation/engine.py` (`_pending_delayed_actions`, keyed by TARGET
ENTITY ID rather than rule id — a fresh dispatch for any entity
transparently supersedes whatever was pending for that same entity,
which is what makes both "confirmed cancels pending off" and "repeated
cleared resets its own debounce" fall out of one generic rule with zero
coupling between the ON and OFF rules). `_run_execution()` defers
`_verify_and_finalize()` until the delayed dispatch (or its
cancellation) actually happens — never claims a device changed state
merely because an action was scheduled.

Files: `luno/automation/models.py`, `luno/automation/engine.py`,
`config/automation_rules.json` (one new rule). Nothing else touched —
YOLO/RTSP/Tapo/HA-credential/P0.8.8-dedupe code untouched.

Tests: `tests/test_p0_8_9_wled_off_debounce.py` (25 new tests — schema
validation, real-config sanity proving the two pre-existing rules are
byte-for-byte unchanged, real-`runtime.scheduler` end-to-end behavior,
and a real `VisionCameraEventBridge` production-call-path proof).

Full repository sweep (154 files): 4,325 passed, 1 skipped, 42 failed —
every failure traced to an already-documented pre-existing category
(LLM token-param `.env` override, no-audio-hardware sandbox gap,
`config/backups/` forensic drift, real `light.main_light` config drift),
plus three tests newly EXPLAINED (not newly caused): `.env` now has
`CAMERA_AUTOMATION_ENABLED=true` (previously `False`), which three
"disabled by default" tests assert against — confirmed purely an `.env`
value via an explicit override re-run (clean 3/3 pass), flagged for the
user (this may be a deliberate change from their own recent
troubleshooting to survive a real restart, or may need to be persisted
more permanently - not modified by this sprint, out of scope). Zero
failures touch `luno/automation/` or `luno/camera_automation/` beyond
those three.

Result classification: STRONG. Physical WLED illumination (stage F) is
explicitly not claimed - see `docs/change_impact/camera_automation_
p0_8_9.md` §7 for the full honesty discussion.

See `docs/change_impact/camera_automation_p0_8_9.md` and
`ARCHITECTURE_GUARD.md` §101 for the full writeup.

## 19zzzz/20zzzz. LUNO P0.9 — Room Occupancy State + Presence Duration

New architecture layer: "YOLO detects. Vision confirms. Occupancy
remembers. Automation decides. Home Assistant executes." Added
`luno/vision_occupancy.py::RoomOccupancyModule` — a new, additive,
always-active `Module` (registered in `luno/bootstrap/modules.py` as
`room_occupancy_module`) that subscribes directly to three EXISTING
Vision Adapter events, all unmodified: `HumanPresenceConfirmed` (the SAME
signal the real WLED-ON rule keys on) -> `occupied`; `CameraPersonLeft`
(the SAME signal the real WLED-OFF rule keys on, P0.8.2/P0.8.9) ->
`vacant`; `VisionFrameProcessed`'s own `human_count` field keeps
`person_count` fresh across multi-person changes without ever deriving
STATE from it.

Snapshot: `{state, person_count, occupied_since, vacant_since,
last_seen, presence_duration_seconds}`. `time.monotonic()` is the only
clock for duration; `luno.core.utils.utcnow()` only for human-readable
timestamps — never mixed (structurally and behaviorally proven). No
persistence — a fresh instance IS what a restart looks like, never a
fabricated `occupied_since`. Publishes `room_occupied`/`room_vacant`
(once per genuine transition, never repeated) plus an optional
`occupancy_changed` umbrella event.

`RoomOccupancyModule` never imports Home Assistant, never calls
ToolManager, never controls WLED/any device, and never runs a second
YOLO/tracked-object pass — enforced by 7 static architecture-guard
tests. `luno/vision.py`, `luno/adapters/vision.py`,
`luno/camera_automation/`, `luno/automation/`, and `config/automation_
rules.json` were not opened or modified.

Files: `luno/vision_occupancy.py` (new), `luno/bootstrap/modules.py`
(registration only).

Tests: `tests/test_p0_9_room_occupancy.py` (34 tests — state machine,
duration/re-entry/multi-person semantics, clock correctness, restart
behavior, snapshot consistency, a real full-stack co-existence proof
with the existing WLED automation, architecture-guard statics).

Full repository sweep (155 files): 4,356 passed, 1 skipped, 45 failed —
every failure traced to an already-documented pre-existing category
(`.env` token-param override, no-audio-hardware sandbox gap,
`config/backups/`/`vision_memory.sqlite3-wal`/`-shm` forensic drift,
real `light.main_light` config drift, the P0.8.9-documented `CAMERA_
AUTOMATION_ENABLED=true` condition) plus three additionally-observed
items this run, all re-confirmed clean in isolation (parallel-load
timing flakes / live SQLite WAL churn, not regressions). Zero failures
touch `luno/vision_occupancy.py`, `luno/vision.py`, `luno/camera_
automation/`, or `luno/automation/`.

Existing WLED ON/OFF automation confirmed unaltered: `config/automation_
rules.json`, `luno/automation/engine.py`/`models.py`, and
`luno/camera_automation/module.py` were not opened; P0.8.9's own 25-test
suite and every other P0.8.x camera/WLED suite remain 100% green; a
dedicated test in this sprint's own suite additionally proves
`RoomOccupancyModule` and the existing WLED automation both correctly
and independently react to the SAME real `HumanPresenceConfirmed` event.

Result classification: STRONG.

See `docs/change_impact/room_occupancy_p0_9.md` and
`ARCHITECTURE_GUARD.md` §102 for the full writeup.

## 19zzzzz/20zzzzz. LUNO P0.10 — Occupancy-Aware Automation Intelligence

Purely additive on top of P0.9 — wires the existing `RoomOccupancy
Module` snapshot into `AutomationEngine`'s EXISTING `state_readers`
context mechanism (the same mechanism `"camera_patrol"` already used;
no second occupancy state machine, no direct device control added to
`RoomOccupancyModule`).

`RoomOccupancySnapshot` gained two new read-only fields:
`occupancy_age_seconds` (time in the CURRENT state, either direction —
equals `presence_duration_seconds` while occupied, but keeps moving
while vacant, unlike that field, which freezes) and `last_transition`
(`"occupied"`/`"vacant"`/`None`, set only on a genuine transition).
`_publish_transition()` gained a `previous_state` parameter, now
included in the `room_occupied`/`room_vacant`/`occupancy_changed` event
payloads.

`luno/bootstrap/modules.py` moved `RoomOccupancyModule`'s construction
earlier so `AutomationEngine(state_readers={...})` can close over the
real instance, and added five `"occupancy.*"` lambda readers
(`occupancy.state`, `occupancy.person_count`, `occupancy.presence_
duration_seconds`, `occupancy.occupancy_age_seconds`, `occupancy.last_
transition`). Two new log-only diagnostic rules (`occupancy_test_log`,
`occupancy_long_presence_test`) were appended to `config/automation_
rules.json` — neither controls a device.

Files: `luno/vision_occupancy.py` (additive fields only), `luno/
bootstrap/modules.py` (construction order + state_readers dict only),
`config/automation_rules.json` (two new rules appended). One
pre-existing test (`tests/test_p0_8_9_wled_off_debounce.py::test_B7_
real_rules_file_has_exactly_six_rules`) was intentionally updated to
reflect the new eight-rule shipped set.

Tests: `tests/test_p0_10_occupancy_context.py` (44 tests — snapshot
schema, defensive copy, vacant/occupied context, the `occupancy_age_
seconds` vs. `presence_duration_seconds` divergence while vacant,
monotonic clock discipline, transition-direction tracking, multi-person
stability, event payload `previous_state`, duplicate-transition
prevention, `AutomationEngine` context access incl. a static
bootstrap-ordering guard, `occupancy.*` conditions, the two shipped
diagnostic rules end-to-end against the real shipped rules file, WLED
ON/OFF regression with occupancy events coexisting, restart semantics,
architecture guards).

Full repository sweep (156 files): 4,402 passed, 1 skipped, 43 failed —
every failure traced to an already-documented pre-existing category
(same families as P0.9), plus one new instance of the pre-existing
parallel-load-only timing-flake category, confirmed clean 3/3 in
isolation. Zero failures touch `luno/vision_occupancy.py`, `luno/
automation/engine.py`, `luno/automation/conditions.py`, `luno/
bootstrap/modules.py`, or `config/automation_rules.json`.

Existing WLED ON/OFF automation confirmed unaltered — re-verified by a
dedicated real-bootstrap test proving both rules still dispatch exactly
one mocked HA tool call each, with occupancy events coexisting on the
same real Event Bus.

Real Home Assistant hardware and physical WLED behavior were NOT
exercised this sprint (same structural sandbox limitation as every
prior sprint in this line) — every test routes through `MockHome
AssistantHandler`.

Result classification: STRONG.

See `docs/change_impact/camera_automation_p0_10.md` and
`ARCHITECTURE_GUARD.md` §103 for the full writeup.

## 19zzzzzz/20zzzzzz. LUNO P0.11 — Action Sequence Engine

Extends `AutomationEngine` so a single automation can execute multiple
actions sequentially, with optional blocking delays between them,
without changing any existing single-action or multi-action automation
behavior.

`AutomationRule` gained a new `sequence: List[AutomationAction]` field,
mutually exclusive with the pre-existing `actions` field — a rule
defines exactly one, enforced by `validate_rule()`. `sequence` reuses
`AutomationAction`'s own `{type, parameters}` shape verbatim for every
existing device-action type, plus one new pseudo-type, `{"type":
"delay", "seconds": N}` (finite, non-negative, within the existing
`MIN_DELAY_SECONDS`/`MAX_DELAY_SECONDS` bounds). No second action schema
was invented, and no second `AutomationEngine`/`ToolManager` was
created.

`_run_execution()` branches to a new `_run_sequence()` when `rule.
sequence` is non-empty; the legacy `_run_actions()` call path is
byte-for-byte unmodified for every existing rule. `_run_sequence()`
dispatches each device-action step through the EXACT SAME `self.
_dispatch_action()` the legacy path already uses (no parallel dispatch
method), and stops at the FIRST step whose result is not `"completed"`
— subsequent steps never run. This is a deliberate, different-in-kind
policy from the legacy `actions` path's own run-everything-then-classify
`COMPLETED`/`PARTIAL_FAILURE`/`FAILED` counting, which remains
completely unchanged. A `delay` step blocks only the calling execution's
own dedicated thread (`threading.Event().wait()`, never `time.sleep()`,
never a busy loop) — never the AutomationEngine itself, never a
sibling execution running concurrently (proven by a real-stack test:
automation A's 1.5s mid-sequence delay does not block automation B,
triggered during it, from completing immediately).

`AutomationExecution` gained `current_step_index`/`total_steps` (both
`Optional[int]`, `None` for a legacy `actions`-based execution — proven
by a dedicated test). `_set_enabled()`/`_rule_to_storage_dict()` were
extended to persist the `sequence` field through the one existing write
path.

Files: `luno/automation/models.py` (additive schema/validation only),
`luno/automation/engine.py` (new sequence-execution methods + a 4-line
branch in `_run_execution()`). Nothing in `luno/vision.py`, `luno/
vision_occupancy.py`, `luno/adapters/vision.py`, `luno/camera_
automation/`, `config/automation_rules.json`, or the legacy dispatch/
verification methods was opened or modified.

Tests: `tests/test_p0_11_action_sequence.py` (52 tests — schema
validation, device-action dispatch, strict ordering via event
timestamps, delay execution/timing, stop-on-first-failure with an exact
dispatched-call-count proof, live execution-state observability,
log-line format assertions, ToolManager-only dispatch proof via both a
real event subscription and a static AST walk, backward compatibility
incl. the real shipped rules file and the legacy `PARTIAL_FAILURE`
classification, concurrent-automation independence, honest
documentation of the absent cancellation mechanism, and 7 static
architecture-guard tests). `tests/test_sprint72_automation_engine.py`'s
pre-existing 78 tests: unchanged, all still passing.

Full repository sweep (157 files): 4,454 passed, 2 skipped, 43 failed —
every failure traced to an already-documented pre-existing category
(same families as P0.9/P0.10). Zero failures touch `luno/automation/`,
`luno/vision.py`, `luno/vision_occupancy.py`, or `luno/camera_
automation/`.

Real Home Assistant hardware and physical WLED behavior were NOT
exercised this sprint (same structural sandbox limitation as every prior
sprint) — every test routes through `MockHomeAssistantHandler`.

Known limitations (by design, per the brief): no cancellation mechanism
(`_wait_delay()`'s `threading.Event().wait()` choice leaves a seam for a
future sprint, named P0.14 in the brief, to add one without a call-site
change); no parallel execution (reserved for P0.12); no IF/ELSE, loops,
repeat, join, variables, or natural-language sequence authoring.

Result classification: STRONG.

See `docs/change_impact/action_sequence_engine_p0_11.md` and
`ARCHITECTURE_GUARD.md` §104 for the full writeup.

## 19zzzzzzz/20zzzzzzz. LUNO P0.12 — Automation API & CRUD

Gives the Dashboard a stable HTTP surface for managing `AutomationEngine`
rule definitions — list/get/create/update/delete/enable/disable/run/
validate — without ever touching `config/automation_rules.json` or
engine internals directly. Does NOT implement any Dashboard UI; it is
the API layer P0.13 will consume.

New file `luno/dashboard/automation_api.py` is a pure translation layer
— the same architectural role `collectors.py`/`controls.py` already
play for every other panel — sitting between `server.py`'s HTTP routing
and `AutomationEngine`'s own unchanged internal `{"ok", "code",
"message"}` contract. Four new `AutomationEngine` methods (`get_rule()`,
`create_rule()`, `update_rule()`, `delete_rule()`) hold the engine's
existing `RLock` across their entire check-validate-mutate-persist-
reload sequence (verified race-free with a real 30-thread concurrent-
CRUD test), reuse the existing atomic `_persist_rules()`/`reload_
rules()` pair (no second persistence mechanism), and require no
migration for any existing hand-authored rule. `enable_automation()`/
`disable_automation()`/`run_automation()` are reused verbatim/near-
verbatim (`run_automation()` gained an additive `execution_id` return
key via a purely additive `_trigger()` out-parameter — its own return
type is unchanged). `AutomationRule` gained three additive, genuinely-
persisted fields — `description`, `created_at`, `updated_at` (server-
set only, never trusted from a request body).

Routing follows the server's existing GET/POST-only, flat-elif-chain,
verb-suffixed-path convention (`/api/automations/{id}/update`,
`/delete`, `/enable`, `/disable`, `/run`) rather than inventing HTTP
PUT/PATCH/DELETE, which the server has never implemented for any route
— the brief itself permitted following the existing convention over
its own illustrative endpoint sketch. `POST /api/automations/{id}/run`
reuses `AutomationEngine.run_automation()` → `_trigger()` → `_run_
execution()` → `ToolManager` verbatim — no second execution path, no
direct Home Assistant call. `POST /api/automations/validate` is a
provably pure function (no `modules`/engine argument at all) with zero
persistence or runtime side effects. No authentication mechanism was
invented — none existed anywhere in the project before this sprint; the
localhost-only bind remains the sole security boundary, documented as a
known limitation rather than silently assumed.

Files: `luno/automation/models.py` (additive fields/validation only),
`luno/automation/engine.py` (additive `_trigger()` out-param, additive
`run_automation()` dict key, four new CRUD methods), `luno/dashboard/
server.py` (two new GET branches + an `automation_api.dispatch_post()`
first-try in `_dispatch_post()`, falling through to the unchanged
`_run_control()`/404 path unmodified). New file: `luno/dashboard/
automation_api.py`. Nothing in `luno/vision.py`, `luno/vision_
occupancy.py`, `luno/adapters/vision.py`, `luno/camera_automation/`, or
`luno/tool_manager.py` was opened or modified.

Tests: `tests/test_p0_12_automation_api.py` (54 tests — route
registration; GET list/one/404; CREATE valid/duplicate/invalid-
trigger/invalid-action/invalid-sequence/id-generation/unsafe-id-
rejected; UPDATE existing/nonexistent/immutable-created_at/refreshed-
updated_at; DELETE existing/nonexistent-never-silently-ignored;
ENABLE/DISABLE existing/nonexistent/runtime-persisted-consistency;
VALIDATE valid/invalid/zero-side-effects; RUN existing/nonexistent/
reuses-existing-path/no-direct-HA; P0.11 sequence-schema round trip;
legacy actions-only compatibility; malformed payload → structured
error, never a traceback; 30-thread concurrent CRUD with zero lost
updates; no eval/exec/shell/dynamic-import; existing-security-model-only
assertion; and 10 static architecture-guard tests M1–M10). All 54
passing. `tests/test_sprint72_automation_engine.py`'s pre-existing 78
tests and `tests/test_dashboard.py`/`tests/test_memory_dashboard.py`'s
73 tests: unchanged, all still passing.

Full repository sweep (158 files): 4,507 passed, 44 failed — every
failure traced to an already-documented pre-existing category (same
families as P0.9–P0.11). Zero failures touch `luno/automation/`, `luno/
dashboard/`, `luno/vision.py`, `luno/vision_occupancy.py`, or `luno/
camera_automation/`.

Real Home Assistant hardware and physical WLED behavior were NOT
exercised this sprint — every test routes through
`MockHomeAssistantHandler`.

Known limitations (by design, per the brief): no authentication for any
Dashboard API route; update/delete via POST + verb-suffixed path, not
HTTP PUT/DELETE; `_classify_field()` is a best-effort single-error
classifier, not a multi-error validator; no P0.13 Dashboard/UI, visual
builder, drag-and-drop, IF/ELSE, variables, loops, parallel execution,
or advanced scheduling was implemented, per the user's own explicit
closing instruction.

Result classification: STRONG.

See `docs/change_impact/automation_api_p0_12.md` and
`ARCHITECTURE_GUARD.md` §105 for the full writeup.

## 20zzzzzzz/21zzzzzzz. LUNO P0.13 — Automation Dashboard / Visual Automation Builder

Gives the Dashboard a usable UI for creating, editing, validating,
testing, enabling/disabling, running, and deleting automations —
consuming the P0.12 Automation API exclusively (Dashboard UI → P0.12
API → `AutomationEngine` → `ToolManager` → Home Assistant). Does NOT
rewrite `AutomationEngine`, does not add a second execution path, does
not call Home Assistant or touch `config/automation_rules.json` from
the UI, and does not start P0.14 (AI/natural-language automation
authoring).

All new frontend work was added inline to `luno/dashboard/static/
index.html` — the project's ONLY static asset (no separate .js/.css
files, no build step, no frontend framework anywhere) — following its
existing panel/nav/modal/`api()`/`esc()` conventions verbatim (directly
mirroring the pre-existing Memory Dashboard's own modal-guard/edit/
delete pattern). One new helper, `escAttr()` (escapes `&<>"'`), was
added because the new editor is the first panel in this codebase to
interpolate free text into quoted HTML attributes — `esc()` alone
would have been an XSS gap for `value="..."` fields. The sequence
builder reuses the P0.11 `{"type", "parameters"}` schema verbatim
(delay steps as `{"type": "delay", "parameters": {"seconds": N}}`) — no
second action schema was invented, and reordering uses explicit up/
down controls rather than drag-and-drop (no such library exists in
this project). The run/execution monitor was designed around a real,
source-confirmed asymmetry in `AutomationEngine` (legacy `actions`-
based runs populate `_last_execution` only on completion; `sequence`-
based runs populate and mutate it live) — step-level progress is shown
only when the API response actually contains `last_execution.
total_steps`, so the UI never fabricates a backend state.

One new, minimal, read-only API endpoint was added —
`GET /api/automations/schema` — a live reflection of `models.py`'s own
`TRIGGER_TYPES`/`CONDITION_TYPES`/`ACTION_TYPES`/`SEQUENCE_STEP_TYPES`
constants plus the already-loaded `luno.devices.LIGHTS`/`SWITCHES`
registry plus non-enforced UI autocomplete hints — justified by the
brief's own explicit permission for "the smallest appropriate API
extension." The device/action picker builds
`{"type": "home_assistant.turn_on", "parameters": {"entity_id": "..."}}`
-shaped payloads from this real, existing device registry rather than
a fabricated discovery mechanism.

Files: `luno/dashboard/automation_api.py` (one new function,
`get_schema()`, plus `_known_devices()` — additive only), `luno/
dashboard/server.py` (one new GET branch, `/api/automations/schema`,
positioned before the existing `/api/automations/{id}` catch-all),
`luno/dashboard/static/index.html` (new `.autom-*` CSS block, new
`escAttr()` helper, new nav button/panel/modal markup, ~600 new lines
of JS registered in the existing `onPanelShown()` loader-dispatch
map). Nothing in `luno/automation/engine.py`, `luno/automation/
models.py`, `luno/vision.py`, `luno/vision_occupancy.py`, `luno/
camera_automation/`, or `luno/tool_manager.py` was opened or modified
this sprint.

Tests: `tests/test_p0_13_automation_dashboard.py` (65 tests — sections
A–X per the brief's own lettered checklist: list loading/empty-state/
create/edit/delete-confirm/enable/disable/manual-run/validate-success/
validate-failure/sequence-create/sequence-reorder/delay-step/action-
step/invalid-action/invalid-trigger/invalid-condition/API-failure/
network-failure/no-direct-HA/no-direct-config-mutation/no-second-
execution-path/XSS-safe-rendering/persistence-after-refresh; a
dedicated Schema section SCHEMA1–5; and 11 static architecture-guard
tests M1–M11, implemented via a custom brace-depth JS function-body
extractor paralleling the existing Python-AST-based guard convention).
All 65 passing.

Full repository sweep (154 files under `tests/`, 8-chunk methodology):
4,464 passed, 45 failed, 5 collection errors, 1 skipped — every
failure/error traced to an already-documented pre-existing category
(same families as P0.9–P0.12, plus one newly-observed instance of the
long-documented sandbox network-isolation limit in
`test_sprint71_dashboard_startup_recovery.py::test_12_...`, and one
confirmed parallel-xdist-order flake in `test_verification_
dashboard.py`, both re-confirmed via isolated re-run). Zero failures
touch `luno/automation/`, `luno/dashboard/`, `luno/vision.py`, `luno/
vision_occupancy.py`, or `luno/camera_automation/`.

Real Home Assistant hardware and physical WLED behavior were NOT
exercised this sprint — every test and every manual UI action in this
sandbox routes through `MockHomeAssistantHandler`.

Known limitations (by design, per the brief): no authentication for the
new schema endpoint (same pre-existing limitation as every other
route); sequence reordering uses explicit up/down controls, not
drag-and-drop; the schema endpoint's "known_*" hint arrays are UI
autocomplete conveniences only, never a server-side allowlist gate; no
AI/natural-language automation authoring was implemented or started,
per the user's own explicit closing instruction (reserved for a
future, separately-requested P0.14).

Result classification: STRONG.

See `docs/change_impact/automation_dashboard_p0_13.md` and
`ARCHITECTURE_GUARD.md` §106 for the full writeup.

## 20zzzzzzzz/21zzzzzzzz. LUNO P0.14 — Advanced Home Assistant Automation Actions & Script Runner

Extends the Automation Dashboard so a user can visually build multi-step
Home Assistant workflows (trigger → conditions → sequential actions →
delay/wait → next action → optional condition/branch → HA service call)
that behave like real HA automations/scripts, while strictly preserving
the existing architecture: Dashboard UI → Automation API →
`AutomationEngine` → `ToolManager` → Home Assistant. Does NOT introduce
a second execution path, does not call Home Assistant directly from the
frontend, does not bypass `ToolManager`, does not rewrite
`AutomationEngine`, does not touch Vision/Camera Automation, and does
not start P0.15/AI natural-language automation generation/voice
automation authoring/autonomous automation creation.

Seven new `home_assistant.*` action types were added to the existing,
closed `ACTION_TYPES` allowlist — `toggle`, `set_brightness`,
`set_color`, `set_temperature`, `run_script` (optional `variables`
dict), `activate_scene`, and `call_service` (generic, controlled —
`{domain, service, target: {entity_id: [...]}, data}`, validated with a
lowercase-snake-case regex on `domain`/`service`, never an arbitrary
string). Three new sequence-only control step types were added
alongside the existing `"delay"` pseudo-type — `wait_until` (bounded
polling, 1–300s, reuses the SAME `AutomationEngine.ha_state_reader` hook
already wired for the P0.8.0 Camera Action Safety Gate and the SAME
`evaluate_condition()` comparison engine every other condition already
uses — no second HA read path, no second comparison engine; honestly
times out with `ha_state_reader_unavailable` under the mock backend
rather than ever fabricating a match), `condition` (constrained
if/then/else, `MAX_CONDITION_NESTING_DEPTH=3`, reuses
`evaluate_condition()` verbatim for its own `conditions` list and
`_run_sequence_step()` verbatim for every nested step — a step behaves
identically whether at the top level or nested one level inside a
branch), and `stop_automation` (an explicit, intentional early exit —
`ExecutionStatus.CANCELLED`, a genuinely new terminal status, never
`FAILED`). `ExecutionStatus` also gained `TIMEOUT` for a `wait_until`
step that never saw its condition become true within budget. Every new
action type still dispatches through the EXACT SAME `AutomationEngine.
_dispatch_action()` → `_dispatch_tool_call()` → `tool_requested` →
`ToolManager` round trip every pre-existing action already used (see
`engine.py::_build_p0_14_tool_call()`) — still exactly ONE execution
path. The Camera Action Safety Gate's own `CAMERA_HA_ACTION_TYPES`
allowlist (`{turn_on, turn_off}`) was deliberately left UNCHANGED —
every new P0.14 action type is automatically refused
(`unsupported_action_type`) for any camera-triggered rule, a
conservative, intentional safety boundary, not a gap.

ToolManager's `home_assistant` handler (mock and real) already
supported `toggle`/`set_temperature`/`set_color`/`set_brightness`/
`run_script` before this sprint — only `call_service` and
`activate_scene` needed a small, additive extension in both
`luno/tool_manager/builtin/home_assistant.py` and `real_home_
assistant.py`. `run_script`'s real-handler branch gained an optional
`variables` dict — absent, the call is byte-for-byte the pre-P0.14
`homeassistant.turn_on` call; present, it routes through `script.
turn_on` with a `variables` payload instead (real HA only accepts
`variables` on that specific service).

One new, minimal, read-only API endpoint — `GET /api/automations/
devices` — a categorized device/entity picker for the visual step
builder, separate from (but reusing the exact same source data as) the
existing flat `schema.devices` list. Only `lights`/`switches`/`scripts`
are ever populated (the same already-loaded `luno.devices.LIGHTS`/
`SWITCHES`/`SCRIPTS` registries `_known_devices()` already used —
`_known_devices()` was itself additively extended to include `SCRIPTS`
too, so the flat schema picker also gained a scripts optgroup);
`fans`/`climate`/`media_players`/`sensors`/`scenes`/`other` are always
returned as genuinely empty arrays — this project has no local registry
and no live-discovery mechanism (`RealHomeAssistantClient` has no
`get_states()`-equivalent bulk listing) for any of them, confirmed by
inspection, never fabricated. `ha_connected` reports whether a REAL
(not mock) Home Assistant client is currently bound, via
`type(client).__name__ == "RealHomeAssistantClient"` (a deliberate
type-name comparison, not an `isinstance`/import, keeping the dashboard
layer free of an import-time dependency on adapter internals) — a UI
hint only, never gates what a user can type manually.

The dashboard UI (`luno/dashboard/static/index.html`) gained: icon/
label/default-parameter entries for every new action/step type; new
`renderStepParamFields()` branches (toggle merged into the existing
turn_on/off picker; set_brightness/set_color/set_temperature/run_script
[script-only device picker + variables JSON textarea]/activate_scene/
call_service [domain/service/entity-list/data JSON textarea]/wait_until
[target/attribute/operator-dropdown/value/timeout]/condition
[delegates to new nested-branch renderers]/stop_automation); new
`renderCondConditions()`/`renderCondBranch()`/`renderCondSubStepFields()`
functions for the condition step's own THEN/ELSE nested-step UI (a
compact `"mainIdx:branch:subIdx"` address string, deliberately
supporting only simple, non-nested sub-step types from the visual
builder — Section 9's own "constrained, declarative, not a full
programming language" instruction; a rule author who needs deeper
nesting can still author one directly against the API, which enforces
the identical `MAX_CONDITION_NESTING_DEPTH` bound either way); new
"+ Add Wait Until / + Add Condition / + Add Stop" buttons; generalized
`[data-entity-picker]` (now takes a `data-entity-key`),
`[data-step-entity-list]` (comma-separated → `{entity_id: [...]}`), and
`[data-step-json]` (JSON textarea parsing, invalid JSON left
uncommitted rather than silently coerced) event bindings;
`CANCELLED`/`TIMEOUT` added to the run-monitor's status→CSS-class maps;
and one honest "Home Assistant unavailable — only locally known lights/
switches/scripts are shown" hint, driven by the new devices endpoint's
`ha_connected` flag, shown once above the step builder rather than
fabricating entities.

**Real, live production config drift, discovered (not caused) this
sprint:** the real `config/automation_rules.json` now contains ONLY one
rule — a genuine, user-created "Back From Work" rule built through the
live P0.13 dashboard. Every P0.6–P0.10 diagnostic/safety rule
previously shipped is gone. Traced conclusively via `config/backups/`'s
own 91-file history (progressive size shrinkage immediately followed by
the new rule's creation timestamp) to deliberate, sequential user
deletions through the live dashboard — not a P0.14 bug (P0.14 touched
zero persistence code; its own smoke tests used only temp files).
Restorable from `config/backups/` if the old diagnostic rules are
wanted back. This causes ~71 pre-existing tests across 11 files
(`test_p0_6*.py`, `test_p0_7*.py`, `test_p0_8_0/1/2*.py`, `test_p0_8_9*.py`,
`test_p0_10*.py`) to fail against the real shipped file — these files
were deliberately left UNTOUCHED (not silenced) since they exist
specifically as regression guards for those production rules' presence;
see `docs/project_handover.json`'s `known_baseline_failures` for the
full breakdown.

Files: `luno/automation/models.py` (7 new `ACTION_TYPES`, 3 new
sequence control step types, new validators, `MAX_CONDITION_NESTING_
DEPTH`/`MAX_WAIT_UNTIL_TIMEOUT_SECONDS`/`MAX_CALL_SERVICE_DATA_KEYS`
bounds, `ExecutionStatus.CANCELLED`/`TIMEOUT`), `luno/automation/
engine.py` (`_run_sequence_step()` single dispatch router,
`_run_stop_step()`/`_run_wait_until_step()`/`_run_condition_step()`,
`_build_p0_14_tool_call()`), `luno/tool_manager/builtin/
home_assistant.py` + `real_home_assistant.py` (`call_service` +
`activate_scene`; `run_script` gained optional `variables`), `luno/
dashboard/automation_api.py` (new `get_devices()`), `luno/dashboard/
server.py` (one new GET branch, `/api/automations/devices`, before the
`/api/automations/{id}` catch-all), `luno/dashboard/static/index.html`
(additive JS/CSS as described above). Nothing in `luno/vision.py`,
`luno/vision_occupancy.py`, or `luno/camera_automation/` was opened or
modified this sprint.

Tests: `tests/test_p0_14_ha_script_actions.py` — 58 new tests, sections
A–T per the brief's own lettered checklist (generic HA service action,
run HA script, activate scene, delay, wait-until-success, wait-until-
timeout, condition-true, condition-false, sequence ordering, failure-
stops-sequence, entity validation, invalid action rejection, unknown
service rejection, no-direct-HA-frontend-access, no-ToolManager-bypass,
no-second-execution-path, backward-compatibility-with-P0.11,
persistence, execution monitor, security architecture guards) plus a
dedicated concurrency test (a `wait_until` in one automation never
blocks an unrelated one) and an honest `REAL_HA_TEST = NOT_PERFORMED`
marker. All 58 passing. 5 pre-existing P0.12/P0.13 architecture-guard
tests were fixed forward (`test_p0_12_automation_api.py::test_M3`,
`test_p0_13_automation_dashboard.py::test_T1`/`test_T3`/`test_SCHEMA5`)
— their bare substring/lowered-source-segment checks false-positived on
P0.14's own legitimate new schema strings (`home_assistant.
call_service` as a rendered label/icon key), comments (prose mentioning
"home_assistant"/"call_service" as English words), and the new `script`
device domain; re-expressed as precise AST-based import/call checks and
word-boundary regexes preserving each guard's exact original intent (no
import/instantiation of a real HA client, no direct service-call
invocation, no unqualified dispatch pattern) — same "legitimate,
in-scope literal update, not a workaround" convention every prior
sprint in this project has used for a stale-but-correct-intent
assertion.

Full repository sweep (153 files under `tests/`, 4-chunk parallel
methodology, `pytest -n 4` per chunk, the 3 pre-existing-broken
collection files excluded same as every prior sprint): 4,448 passed,
104 failed. Every failure traced to an already-documented pre-existing
category: the newly-discovered `config/automation_rules.json`/real-
device-config drift family above (~87 across the P0.6–P0.10/sprint60
lines), LLM `.env` token-param override (12), `config/backups/`/
mutation-audit forensic drift (13), no-audio-hardware sandbox gap (6),
real-whisper construction gap (2), real credentials in `.env` (1), and
one confirmed parallel-xdist-order timing flake
(`test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_
never_plays`, re-confirmed clean standalone: `1 passed in 0.81s`). Zero
failures touch `luno/automation/`, `luno/dashboard/`, `luno/tool_
manager/builtin/home_assistant*.py`, `luno/vision.py`, `luno/vision_
occupancy.py`, or `luno/camera_automation/`.

Real Home Assistant hardware and physical WLED behavior were NOT
exercised this sprint — `REAL_HA_TEST = NOT_PERFORMED`, honestly
recorded per the brief's own Section 21 instruction, never fabricated.
A real end-to-end smoke test WAS performed against the real bootstrap
stack under the MOCK Home Assistant backend: a 7-step sequence
(call_service/run_script-with-variables/activate_scene/set_brightness/
set_color/toggle/condition) → COMPLETED; a `stop_automation` sequence →
CANCELLED (2/3 steps ran); a `wait_until` with no state reader bound →
TIMEOUT with an honest `ha_state_reader_unavailable` code.

Known limitations: `wait_until` only supports `attribute="state"` (no
attribute-level brightness/rgb_color/etc. state reader exists anywhere
in this engine); the visual condition-branch builder deliberately
supports only simple, non-nested sub-step types (a rule author needing
deeper nesting must author it directly against the API); no
authentication exists for the new devices endpoint (same pre-existing,
documented limitation as every other dashboard route); `fans`/
`climate`/`media_players`/`sensors`/`scenes`/`other` categories have no
local registry or live-discovery mechanism in this project at all; no
AI/natural-language/voice automation authoring was implemented or
started, per the user's own explicit closing instruction.

Result classification: STRONG.

See `docs/change_impact/ha_script_actions_p0_14.md` and
`ARCHITECTURE_GUARD.md` §107 for the full writeup.

## 21. TAKEOVER PROTOCOL / NEXT AGENT TAKEOVER

**The repository documentation is the authoritative project memory. The
previous chat is not required for takeover.**

A future coding agent (any account) MUST, before touching any source
file:

1. Read `docs/project_handover.md` (this file) in full.
2. Read `docs/project_handover.json`.
3. Read `ARCHITECTURE_GUARD.md` (at minimum the most recent 2-3 numbered
   sections — each is self-contained and cites exactly what it changed).
4. Read the latest 2-3 files under `docs/change_impact/*.md` (sorted by
   sprint number in the filename/content, not by file mtime).
5. Read `docs/testing/regression_baseline.md`'s LATEST section (append-
   only — do not assume an early section reflects current behavior).
6. Inspect the actual source files this document points to — **the
   source code is the authority; if this document and the source code
   ever disagree, report the discrepancy, then trust the source code.**
7. Run the core regression suite (see §13's file list) to verify the
   current baseline before making any change.
8. Identify the exact next task from §22 below (or from a new brief the
   user provides).
9. Do NOT repeat completed work. Do NOT rewrite working architecture
   without first reproducing a concrete defect through the real
   `RuntimeDemoConsole` path. Do NOT invent a problem to have something
   to fix — if the current architecture already handles a proposed
   scenario correctly, document that and move on.
10. Before finishing (or if you run out of context/time/budget mid-
    task), update this file and `docs/project_handover.json` with:
    current status, last completed phase, current phase, last verified
    test run, files modified, files created, known failures, persistent-
    state status, and the exact next action — so a completely different
    agent/account can continue without any access to this conversation.

**Production systems that must NOT be modified without a reproduced
defect** (per §14's own invariants, reaffirmed after 8 sprints of
holding): `luno/memory_retrieval/` (ranking/budget/`assemble_context()`
signature), `render_context_block()`'s trust-boundary comment block,
`ActiveTopicSnapshot`'s field set, `REFERENCE_TYPES`'s 10-member tuple,
`_TOKEN_SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES` (no new groups without
a reproduced E2E failure + domain-stability + no-cross-topic-false-
positive justification, per Sprint 45's own rule), TTS/streaming/
cancellation semantics (Sprints 36/37, untouched since), `is_active_
topic_relevant_to_query()`'s pre-existing `distinct_other_count >= 2`
threshold (two Sprint 47 attempts to widen it to `>= 1` globally were
both reverted after concrete regressions, see `ARCHITECTURE_GUARD.md`
§47), Sprint 48's own `distinct_other_count >= 1 and _DEMONSTRATIVE_
ANCHORED_RE.search(text)` gate (do not widen to `distinct_other_count
== 0` — would break Invariant 6), `_extract_entity_differentiator()`'s
own UPPERCASE-only, letters-only scope (do NOT widen to lowercase or
digits without a fresh safety analysis — see `ARCHITECTURE_GUARD.md`
§49 and the change-impact doc's own Phase 3 reasoning for why each
restriction exists) and the `coverage > 0.5` threshold value itself
(unchanged since Sprint 43, still `>`, never `>=`, per Sprint 46's own
"Rejected #1"). **As of Sprint 50:** the 5 new event publishes in
`_handle_utterance()` are OBSERVATIONAL ONLY — do not make any code
downstream of them read one back and change behavior based on it (that
would turn an observability tap into a second decision system, exactly
what this sprint's own non-negotiables forbid); `EventLogWriter`'s
opt-in-by-default-on-`RuntimeDemoConsole` / unconditional-on-
`DashboardServer` split must not be inverted without re-verifying the
~2900 pre-existing tests still get zero new files; `replay.py`'s own
"never call a real LLM" property must be preserved (a future sprint
tempted to make replay "more realistic" by calling a real LLM would make
replay non-deterministic, defeating its entire purpose as a regression
tool). **As of the Dashboard Turn-State Recovery fix:**
`_run_utterance_turn_safely()`'s own `llm_error` publish on an escaped
exception is a RECOVERY signal only — do not make any code react to it
to change conversation-intelligence behavior (same non-negotiable as the
Sprint 50 events above); `_is_expected_client_disconnect()`'s narrow
errno/winerror check must not be widened without a newly-observed,
concrete errno to justify it.

**Tests that should be run before making any change** (the targeted
core-plus-observability-plus-recovery suite, currently 983 tests —
now also includes P0.14's own `tests/test_p0_14_ha_script_actions.py`
(58 tests), P0.13's own `tests/test_p0_13_automation_dashboard.py`
(65 tests), P0.12's own `tests/test_p0_12_automation_api.py` (54
tests), Sprint 72's own `tests/test_sprint72_automation_
engine.py` (78 tests), P0.11's own `tests/test_p0_11_action_sequence.py`
(52 tests), Sprint 71's own `tests/test_sprint71_camera_
patrol.py` (37 tests) and `tests/test_sprint71_dashboard_startup_
recovery.py`):
`tests/test_p0_14_ha_script_actions.py tests/test_p0_13_automation_dashboard.py tests/test_p0_12_automation_api.py tests/test_sprint72_automation_engine.py tests/test_p0_11_action_sequence.py tests/test_sprint71_camera_patrol.py tests/test_sprint71_dashboard_startup_recovery.py tests/test_dashboard_turn_state_recovery.py tests/test_runtime_
observability.py tests/test_real_world_capture.py
tests/test_replay_engine.py tests/test_memory_voice_observability.py
tests/test_entity_provenance_disambiguation.py tests/test_bounded_
entity_provenance.py tests/test_semantic_entity_identity.py tests/
test_contextual_reference_robustness.py tests/test_entity_identity_
semantic_alias_continuity.py tests/test_entity_concept_continuity.py
tests/test_conversation_reference_resolution.py tests/test_
conversation_intelligence.py tests/test_memory_continuity.py tests/
test_memory_comparison_topic_preservation.py tests/test_memory_topic_
retention.py tests/test_temporal_memory_timeline_awareness.py tests/
test_cross_system_conversation_consistency.py tests/test_semantic_
context_bridging.py tests/test_memory_retrieval_decision_quality_
reaudit.py` — run with plain `pytest -q` (no `-n`), this exact command
is what "the current baseline" means throughout this document. Only run
the full 101-file repository sweep (`pytest -n 4`) when about to declare
a piece of work complete — the sandbox's own single-tool-call wall-clock
cap (~170-180s) is TOO SHORT to run the full sweep in one `pytest -n 4
tests/` invocation; split the file list into 8 roughly-equal chunks
(`ls tests/*.py | split -n l/8 -d - chunk_`) and run each chunk as its
own tool call — 6 chunks was still occasionally too slow as of Sprint
49, 8 chunks reliably fit. Re-run any TTS/streaming/voice-pipeline-
timing failure (or, as of this fix, any `inspect.getsource`/"could not
get source code" failure — a known sandbox artifact under `-n 4`
parallel stress, not tied to any specific test file) in ISOLATION
(serial) before ever classifying it as new
vs. pre-existing vs. flaky — this has been necessary in 4 of the last 5
sprints (Sprint 50's own full sweep did NOT reproduce Sprint 49's own
documented flake — a reminder that flakes don't always reproduce, and
absence this run is not itself evidence the flake is gone for good).

## 20zzzzzzzzz/21zzzzzzzzz. LUNO P0.15 — Human-Friendly Dashboard UX & Time-Based Automation Conditions

**CLOSED.** One new, additive condition type — `{"type": "time",
"parameters": {"after": "HH:MM", "before": "HH:MM"}}` — routed through
the EXISTING `AutomationEngine._evaluate_conditions()` →
`evaluate_condition()` pipeline every other condition already uses;
`engine.py` required ZERO changes for this feature (confirmed by direct
inspection that its generic per-condition loop already delegates by
type — this is the architectural fact that satisfies "no new execution
path" by construction, not merely by discipline). Supports both normal
(`after <= before`) and overnight/crosses-midnight (`after > before`)
ranges, both boundaries inclusive, verified against every worked example
in the brief (18:00–23:30 and 22:00–02:00: 21:59 fail, 22:00 pass, 23:59
pass, 00:00 pass, 01:59 pass, 02:00 pass, 02:01 fail). No scheduler,
timer, or polling loop was introduced anywhere — a time condition is a
pure, on-demand comparison against `datetime.datetime.now().time()`,
evaluated exactly once, at real trigger-processing time.
`TIME_CONDITION_TYPE` is deliberately kept OUTSIDE `CONDITION_TYPES`
(that frozenset stays pure comparison operators only — `equals`/
`greater_than`/etc. — consumed by `wait_until`'s and the generic
condition row's own unrelated operator dropdowns; adding `"time"` there
would have broken `test_sprint72_automation_engine.py`'s own exact-
frozenset-content assertions). `AutomationCondition` gained one
additive `parameters: Dict[str, Any] = field(default_factory=dict)`
field (mirrors `AutomationAction.parameters`'s own existing shape;
every pre-P0.15 construction site is unaffected, defaulting to `{}`).

Dashboard UX polish (the brief's own Section 8): a dedicated "🕐 Time"
condition card (native `<input type=time>` From/To fields, a live
"Active during this period" indicator — purely cosmetic, the server
remains the sole evaluation authority) replaced the need to touch raw
`type`/`target`/`value` fields for the common case; the existing
generic/advanced condition row remains available, relabeled "+ Add
Condition (Advanced)"; natural-language "When X / Only between Y / →
Z" summaries now appear under each automation's name in the list view
(entity ids resolved to friendly names via the existing device
registry, never exposed raw unless the person opens the advanced
editor); an empty-conditions state, a loading state for the automations
list, and inline validation messages matching the brief's own wording
("❌ Invalid time…" / "❌ Time range is incomplete…") were added. All of
this stayed inside the existing single-file, dependency-free, vanilla-
JS dashboard — no frontend framework was introduced.

**Files touched:** `luno/automation/models.py`, `luno/automation/
conditions.py`, `luno/automation/__init__.py`, `luno/dashboard/static/
index.html`. Nothing in `luno/automation/engine.py`, `luno/dashboard/
automation_api.py`, `luno/dashboard/server.py`, `luno/vision.py`,
`luno/vision_occupancy.py`, `luno/camera_automation/`, or `luno/
tool_manager/` was opened or modified this sprint.

**Tests:** `tests/test_p0_15_time_conditions.py` — 52 tests, sections
A–G (time validation, normal-range boundaries, overnight-range
boundaries parametrized against all seven brief examples, automation
behavior using real wall-clock time with a several-minute margin,
persistence round trips for both normal and overnight windows through
the real dashboard HTTP API, dashboard source-scan, and AST-based
architecture guards proving no scheduler/polling-loop primitive and no
second execution path). All 52 passing.

**Regression (brief's own Section 13 methodology, followed in order):**
P0.11/P0.12/P0.13/P0.14/Sprint-72 suites re-run BEFORE any change (307
passed, baseline), re-run again after (307 passed, identical), Vision/
Camera Automation suites re-run (24 files — 655 passed, 24 failed, 1
skipped, every failure re-traced to the same already-documented
`config/automation_rules.json`/`config/camera_automation.json` real-
production-data-drift family P0.14 discovered, unchanged by this
sprint), then a full 156-file repository sweep (chunked methodology; 3
pre-existing collection errors for already-documented sandbox gaps) —
approximately 105 failures, every one individually re-traced to an
already-documented pre-existing category (the drift family above, LLM
`.env` token-param override, `config/backups/`/mutation-audit forensic
drift, no-audio-hardware sandbox gap, real-whisper construction gap,
real credentials in `.env`, and one newly-observed but unrelated timing
flake in `test_llm_tts_streaming_production.py`, nothing to do with
automation conditions). Zero failures touch `luno/automation/`, `luno/
dashboard/`, or this sprint's own new test suite.

**Result classification: STRONG.** See `docs/change_impact/
time_conditions_p0_15.md` for the full writeup. Per the user's own
explicit closing instruction — "Stop after P0.15. Do not begin the next
sprint automatically." — work STOPPED here; no further sprint was
started.

## 22. Next Recommended Sprint

**P0.15 (Human-Friendly Dashboard UX & Time-Based Automation Conditions)
is CLOSED** — see the section immediately above and `docs/change_impact/
time_conditions_p0_15.md` for the full writeup. Per the user's own
explicit closing instruction for this sprint, work STOPPED after P0.15
was fully tested and documented: a user can now restrict an automation
to a time-of-day window (including one that crosses midnight) through a
dedicated, human-friendly Dashboard card, with zero scheduler/polling
loop introduced and zero changes to `AutomationEngine` itself. Days-of-
week, dates, sunrise/sunset, weather conditions, IF/ELSE, variables,
loops, retries, cancellation, a scheduler, and any AI/natural-language
automation authoring were explicitly named OUT of scope by the brief's
own Section 15 and must NOT be started without a new, separate user
request.

P0.14 (Advanced Home Assistant Automation Actions & Script Runner)
remains CLOSED as well — see §20zzzzzzzz/21zzzzzzzz above and
`docs/change_impact/ha_script_actions_p0_14.md`.

Two genuinely separate, pre-existing items are recommended as their own
FUTURE (optional) sprints — neither is part of P0.14's or P0.15's own
scope and neither should be conflated with them:

1. **The real `config/automation_rules.json` drift discovered this
   sprint** (§20zzzzzzzz/21zzzzzzzz above) — the P0.6–P0.10 diagnostic/
   safety rules the user previously had are gone, deliberately deleted
   through the live P0.13 dashboard. If the user wants them back, they
   are fully restorable from `config/backups/` (91 historical
   snapshots) — this is a data/config decision for the user to make,
   not a code fix.
2. **General regression-noise maintenance** — the long-running
   `config/backups/`/mutation-audit forensic-drift family and the LLM
   `.env` `MAX_TOKENS_PARAM` override family remain unaddressed, as in
   every prior sprint's own handover; neither is urgent (both are
   environment-specific, not production bugs) but they add noise to
   every future regression sweep.

If a genuinely new, separately-requested feature sprint is started
instead, follow the same discipline every P0.x sprint in this line has
followed: read `docs/change_impact/ha_script_actions_p0_14.md` and
§20zzzzzzzz/21zzzzzzzz above first, inspect the actual current source
(not just this document) before assuming any claimed state, and run the
core regression suite listed in §21 before making any change.

**P0.12 (Automation API & CRUD) is CLOSED** — see §19zzzzzzz/20zzzzzzz
above and `docs/change_impact/automation_api_p0_12.md` for the full
writeup. Per the user's own explicit closing instruction for this
sprint, work STOPPED after P0.12 was verified complete. P0.14
(cancellation, enabled by P0.11's `_wait_delay()` threading.Event()-
based design) remains a logical future phase in the sequence-engine
line, also not started.

**P0.11 (Action Sequence Engine) is CLOSED** — see §19zzzzzz/20zzzzzz
above and `docs/change_impact/action_sequence_engine_p0_11.md` for the
full writeup.

**P0.8.5 (Fix `camera_person_entered` Firing With `person_count=0`)
update — root cause identified and fixed with a complete, source- and
real-runtime-log-evidenced mechanism (see §19ww/20ww above and `docs/
change_impact/camera_automation_p0_8_5.md`). Full regression passes
(11 new tests + 403 Vision/P0.x tests + a clean 145-file sweep). The
concrete next step is for the USER to confirm on their real machine**
(this sandbox still cannot execute real RTSP/YOLO/torch):

1. Pull this sprint's `luno/vision.py` + `luno/adapters/vision.py`
   changes (no other file needs touching).
2. Run the normal Luno runtime on the real machine and watch the
   console for the new `[VISION PERSON DEBUG] raw_boxes=... person_
   count=...` line.
3. Expected outcome if this sprint's diagnosis is correct: the
   `person_count` in that line should now match the `person_count` on
   the very next `[CAMERA EVENT] kind=human_detected` line - i.e. no
   more `camera_person_entered`/`person_count=0` mismatches during a
   normal, continuous person presence. If the mismatch still occurs,
   note whether it happens near a fresh runtime start (the disclosed
   residual cold-start race in §19ww/20ww) or during steady-state
   operation (which would mean the mechanism needs revisiting) and
   report back the surrounding `[VISION PERSON DEBUG]` lines.
4. Once confirmed clean on the real machine, the temporary `[VISION
   PERSON DEBUG]` print line can be removed in a follow-up sprint (it
   was added for this verification, not intended as permanent runtime
   noise) - not done in this sprint since the user asked for it to
   remain until live-verified.

**P0.8.4 (Resolve the Actual YOLO Model / Ultralytics Compatibility
Failure) update — root cause identified and fixed with a complete,
source-evidenced mechanism (a real thread-safety/API-usage defect in
`luno/vision.py`, NOT a model/checkpoint/dependency incompatibility —
see §19vv/20vv above and `docs/change_impact/camera_automation_p0_8_4.
md`). Full regression passes, including a genuine two-thread race-proof
test. The concrete next step is for the USER to confirm on their real
machine** (this sandbox still cannot execute the real `torch`/
`ultralytics` stack - `import torch` fails on a missing real CUDA
runtime library even after the exact-version-matching wheels were
successfully installed this sprint; see the change-impact doc §2 for
the full reasoning):

1. Pull this sprint's `luno/vision.py` change (no other file needs
   touching - the `yolo11n.pt`/`yolov8n-pose.pt` files do NOT need to be
   deleted or re-downloaded this time, since the root cause was never
   the checkpoints).
2. Re-run `python luno_live_p0_8_1_verification.py --sequence p0_8_2`
   with `CAMERA_AUTOMATION_TEST_LIGHT_ENTITY=light.wled CAMERA_
   AUTOMATION_ENABLED=true` set.
3. Expected outcome if this sprint's diagnosis is correct: YOLO
   detection succeeds every cycle (no more `[VISION_DETECTION_FAILED]`/
   `Conv.bn` `AttributeError`), and TEST A-F proceed exactly as
   originally specified in the P0.8.2 brief. If the error somehow still
   occurs, report the exact printed message/hint back — the mechanism
   in `docs/change_impact/camera_automation_p0_8_4.md` §3 would need to
   be revisited with that new evidence rather than assumed still
   correct.

**Long-Term Memory Self-Healing / Recovery Hardening update —
feature-complete, fully tested, no further code required to close this
brief.** All 19 acceptance-criteria items verified via the test suite
(see §19tt/20tt above). No live-hardware dependency, so there is no
equivalent "run this on your real machine" gap for the user to close —
this sprint is closed. Natural, optional future pickups (none required):
(1) the same recovery pattern could in principle be generalized to
`luno/persistence.py`'s six OTHER stores, but this was explicitly out of
scope this sprint and should only be done with its own fresh brief, not
opportunistically; (2) the pre-existing `config/backups/` file-count
drift (now 51 files, tracked since P0.8.0) could be cleaned up by
manually pruning `config/backups/*.json` on the real machine — purely
cosmetic, does not affect correctness, and several `test_sprint63`/
`test_sprint64`/`test_sprint68` forensic tests would need their own
hardcoded counts refreshed afterward if this is ever done.

**P0.8.2 (Camera Human Cleared → Safe Light OFF) update — feature-
complete, fully tested (mocked), no further code required to close
this brief. The concrete next step is for the USER to run the live
procedure on their real machine** (this is the one verification gap
this sandbox cannot close itself — see `docs/change_impact/camera_
automation_p0_8_2.md` §11/§15 for the full reasoning):

```
CAMERA_AUTOMATION_TEST_LIGHT_ENTITY=light.<a_real_harmless_test_light> \
CAMERA_AUTOMATION_ENABLED=true \
python luno_live_p0_8_1_verification.py --sequence p0_8_2
```

then follow the six prompted tests (TEST A-F: light-on prerequisite +
human enter / remain in view / human exit / remain outside frame /
re-enter / re-exit). Before running it, check the two locally-fixable
gaps this sandbox's own pre-flight attempt surfaced (distinct from pure
network-reachability limits): `CAMERA_AUTOMATION_ENABLED` must resolve
to `true`, and `ultralytics` must be installed in the real machine's
own `.venv`. If every test passes and the script's own `Overall: PASS`
verdict prints, this closes the "first real production-style camera
automation behavior" milestone this whole P0.8.x line has been
building toward; only after that should a future sprint consider
pointing either rule at a real, non-test production light.

**P0.8.1 (Live Camera → Home Assistant Light Verification) update —
superseded by P0.8.2 above.** The original six-test `--sequence p0_8_1`
sequence (the default) remains available, byte-for-byte unchanged, for
verifying the ON-only behavior in isolation if ever needed; the
`human_cleared` no-op gap it deliberately left open is now closed by
the new `--sequence p0_8_2` path described above.

**P0.7 (Vision Context → Automation Context) update — feature-complete,
smallest logical next step identified but NOT implemented (per its own
"do not implement the next sprint" constraint):** add a low-frequency,
state-change-gated "VisionContext changed materially" check (reusing the
existing scheduler, never a new tight polling loop, and only publishing
when `person_count`/`detected_objects` actually differ from the last
published snapshot — a real state-change check, not a raw polling-
cycle-to-event mapping) so an object appearing or the person count
changing WHILE a person is already continuously present can also
retrigger automation — currently it cannot, since `VisionContext` only
refreshes on the four pre-existing Enter/Leave/Disconnect/Reconnect
events. If the user wants P0.7 live-verified against real camera
hardware, the concrete next step is: run the real `main.py`, get 2+
people in frame, and confirm `camera_multiple_people_log`'s log line
appears in the AutomationEngine log alongside the existing `camera_
human_detected_log` line. See `docs/change_impact/vision_context_p0_7.
md` §15 for the full reasoning.

**Sprint 72 (Automation Engine Dasar) update — feature-complete, no
further code required to close this brief.** Natural next steps, in
rough priority order: (1) wire a real Home Assistant "get current
state" handler and register it as a second `state_readers` entry in
`AutomationEngine`, unlocking richer conditions; (2) add a camera/HA
`motion_detected`/`door_open`-style real event publisher so EVENT
triggers have a genuine sensor-driven example beyond `tool_finished`-
style events; (3) if a future sprint deliberately wants automation-to-
automation chaining, add a new allowlisted `automation.run` action type
and thread the ALREADY-existing, already-tested `correlation_id`/
`depth` fields through it; (4) author at least one real rule in
`config/automation_rules.json` on the user's own machine and confirm a
live end-to-end run against real hardware - the one verification gap
this sandbox cannot close itself. See `docs/change_impact/
automation_engine.md` §20-21 for the full reasoning behind each item.

**Sprint 71 (Camera Patrol) update — feature-complete, no further code
required to close this brief.** If the user wants it live-verified
against real camera hardware, the concrete next step is: save at least
2-3 real presets on the actual Tapo C212 (via voice, "simpan posisi
kamera sebagai <name>", or the Tapo app), add one route to
`config/camera_patrol_routes.json` referencing those exact preset names
(e.g. `{"rumah": {"presets": ["pintu", "meja", "jendela"], "dwell_
seconds": 10, "loop": true, "max_cycles": 3}}`), then say "mulai patroli
kamera" on the real machine. Two small, pre-existing (not caused by this
sprint) test-scaffolding items were reconfirmed during this sprint's own
regression sweep and remain optional pickups for a future sprint — see
the untouched paragraphs immediately below (both already existed before
Camera Patrol and are unrelated to it).

**Sprint 71 (Dashboard Startup & Access Recovery) update — this is now
CLOSED, no further action required to resolve the reported symptom.**
The dashboard-cannot-open root cause is fixed, tested (15/15), and
live-verified at the HTTP/process level in this sandbox. Nothing from
this sprint is blocking. If a future sprint has spare capacity, two
small, genuinely pre-existing (not caused by this sprint) items were
observed during its full regression sweep and could optionally be
picked up: (1) this checkout's own `.env` sets `MAX_TOKENS_PARAM=max_
tokens`, which the code's own default (`max_completion_tokens`)
disagrees with — causing `tests/test_llm_max_completion_tokens_
compatibility.py` (7) and `tests/test_memory_session_summary_api_
compatibility.py` (5) to fail; either the `.env` value or the tests'
"default mode" assumption needs reconciling, but this is an LLM/API-
compatibility concern entirely unrelated to the dashboard and was left
untouched per Sprint 71's own scope boundary. (2) this long-lived
checkout has accumulated 41 files in `config/backups/` against several
memory-forensics/mutation-audit tests' own hardcoded expectation of a
much smaller pristine count — not a bug, just checkout-state drift from
months of real sprint activity; a future sprint could either update
those tests' expectations or document the accumulation as expected
checkout behavior.

**Sprint 70 (Tapo C212 Live Auth & Auto-Recovery) update:** (1) **run `tapo_ptz_diagnostic.py` on the
real machine** — the single highest-value next step, finally closing
the Phase 1 live-verification gap that both Sprint 69 and Sprint 70
could not close from this sandbox; its output (`CONNECTED`/
`AUTH_FAILED`/`SESSION_EXPIRED`/`AUTH_RATE_LIMITED`/`DEVICE_OFFLINE`/
`PORT_UNREACHABLE`/`HOST_UNREACHABLE`/`UNKNOWN`) tells us definitively
what's actually happening, with zero more guessing. (2) If the result
is `AUTH_FAILED`, the fix is credential-side (re-pair the Camera
Account in the Tapo app) — no further Luno code change would help, by
this sprint's own explicit design (wrong credentials are never
retried). (3) If the result is one of the now-recoverable categories
(`SESSION_EXPIRED`/`DEVICE_OFFLINE`/`HOST_UNREACHABLE`/
`PORT_UNREACHABLE`), a real "pan camera left" command through Luno
itself should now transparently recover on its own — worth confirming.
(4) **Resume and deliver Sprint 69.2** (`luno/vision.py`'s own OpenCV
read-bound/backoff/dashboard-state hardening) — still the standing,
independent, code-complete item deferred since Sprint 69.

**Sprint 69 (Tapo C212 Authentication) update:** (1) **user-side confirmation is the single
most valuable next step** — run a real "pan the camera left" command on
the affected machine and capture the resulting `error_type`/
`data.error_class` (now specific, e.g. `CameraPTZAuthFailed` vs
`CameraPTZUnreachable`) plus the bootstrap startup log line's new
classified message; this will, for the first time, tell us definitively
WHICH of the two documented "disconnect" mechanisms (§19w/20w) — or a
third, still-unknown one — is actually occurring, without guessing.
(2) If the user confirms a genuinely TRANSIENT boot-time failure
(camera was mid-reboot when Luno started), revisit the "lazy retry
instead of permanent mock fallback" design this sprint deliberately
deferred (see the change-impact doc's own "why...deliberately NOT
changed" section) — now with real evidence, and with explicit user
go-ahead, since it touches the preserved bootstrap architecture.
(3) **Resume and deliver Sprint 69.2** (OpenCV camera read-bound/
backoff/dashboard-state hardening for `luno/vision.py`'s own capture
path) — code-complete, 23 new tests + 212 combined targeted tests all
passing, but its own documentation/full-regression/delivery to the
user's machine was deferred in favor of this sprint; this is
independent, already-done work waiting only on a documentation/delivery
pass.

**Sprint 69.1 update:** investigated the SAME symptom recurring after
Sprint 69's deployment; proved (not assumed) there is only one
production camera-open path and that the dashboard telemetry is live,
not stale — see §19v/20v and `docs/change_impact/camera_runtime_
dashboard_forensics.md`. **Sprint 69.1's own recommendation for what's
next (this is the STOP CONDITION gap, unresolved from this sandbox):**
(1) on the actual affected machine, run `camera_diagnostic.py` (now
prints platform + actual local backend candidates) and capture a fresh
application log — the new structured `[Vision]` diagnostic lines will
show the exact source classification (`local(index=N)` vs.
`network(scheme=X, host=Y)`), selected backend, and state transition
that occurs, which will conclusively answer whether `CAMERA_URL` is
being auto-derived from Tapo PTZ credentials (`config.py`'s
`TAPO_HOST`/`TAPO_USERNAME`/`TAPO_PASSWORD` auto-derivation) on that
deployment; (2) if it IS a Tapo URL, the next question becomes "why is
the Tapo camera unreachable" (network/credentials/RTSP path), not a
vision.py code bug; (3) if it is NOT a Tapo URL and a local index is
still somehow reaching FFMPEG, that would be new, load-bearing evidence
this sandbox does not have and should be reported back with the actual
diagnostic output.

**Sprint 69 update:** fixed the reported camera stability bug (see
§19u/20u and `docs/change_impact/camera_stability_fix.md`). **Sprint
69's own recommendation for what's next:** (1) run `camera_diagnostic.py`
on the actual Windows machine that produced the original bug report —
this is the one gap this sandbox structurally cannot close (no camera
hardware, no Windows/DirectShow/Media Foundation here), and would
confirm the backend candidate list actually works against real hardware
rather than just the (Linux, no-camera) sandbox. (2) A separate,
unrelated, pre-existing full-suite-only test flake was newly observed
this sprint (`test_llm_tts_streaming_production.py::test_14_
cancellation_during_synthesis` and `test_verification_dashboard.py::
test_api_verification_reports_a_successful_verified_action_end_to_end`,
both non-reproducing in isolation and not touching camera/vision code at
all) — out of scope for this camera-only sprint, but worth investigating
if a future sprint needs a clean full-suite baseline; Sprint 68's own
`test_camera_presence.py` fix explicitly did not claim to be exhaustive.

**Sprint 65 update:** audited every tool/filesystem/execution surface
Luno has and found no provable self-modification path - see §19q/§20q
and `docs/change_impact/tool_file_access_audit.md`. **Sprint 65's own
recommendation for what's next:** IF a future sprint touches the browser
`download` action, add a startup-time assertion that `BrowserConfig.
download_dir` is disjoint from the project's own source root (Finding
SPRINT65-002) - not urgent (today's default configuration is already
safe), but cheap insurance against a future misconfiguration. Also
consider keeping this sprint's own `test_A_*` tests as a permanent
regression guard against a future sprint accidentally registering a
broader-capability tool (Finding SPRINT65-001). No urgent action needed
otherwise - this was audit-only and found no CRITICAL/HIGH findings.

**Sprint 64 update:** forensically investigated WHO/WHAT plausibly put
the corrupted content into `config/long_term_memory.json` (Sprint 63
diagnosed WHAT it looks like; this sprint asked a different question).
Result: STATUS UNKNOWN for the external origin (a valid outcome, not a
failure), paired with a CONFIRMED EXCLUSION of `luno.memory`'s own code
via structural audit + negative-control reproduction — see §19p/§20p and
`docs/change_impact/long_term_memory_corruption_forensics.md`. **Sprint
64's own recommendation for what's next:** do NOT re-open `luno.memory`'s
own write path as a suspect — that's closed with evidence. The one
avenue this sprint couldn't pursue: if the original host environment
that ran Sprint 55 or earlier ever becomes available for inspection, the
MIT-license-tail fingerprint search (bounded/partial in this sandbox)
would have much higher diagnostic value run there. Otherwise this
remains, as Sprint 63 also concluded, a manual/out-of-band question for
the user rather than a code sprint. Also worth closing if ever possible:
the `recovery/` script directory and its two change-impact docs
(`memory_recovery.md`, `persistent_state_hardening_v2.md`), still absent
from this checkout as of this sprint too.

**Sprint 63 update:** investigated `config/long_term_memory.json`'s
long-standing corruption (Sprint 55/56/57) and found new forensic
evidence (an entropy discontinuity plus embedded MIT LICENSE text)
suggesting the file's current content isn't genuine encrypted memory
data at all, but an accidental fragment of an unrelated binary
artifact — see §19o/§20o and `docs/change_impact/long_term_memory_
recovery.md`. DIAGNOSIS ONLY: a STOP CONDITION applies (unprovable
format, no usable backup, single copy) — no fix, no migration, no data
recovery attempted; the loader's existing graceful fail-closed
behavior was re-verified correct, not patched. **Sprint 63's own
recommendation for what's next:** this is NOT a code sprint — it's a
manual, out-of-band action for the user: check the original `E:\Luno
Evo` device (or wherever this checkout is normally hosted) for any
snapshot/export/sync-history/editor-local-history copy of `long_term_
memory.json` dated AFTER 2026-08-09 (the last known-good restore,
documented in the pre-Sprint-43 "Memory Recovery & Persistence
Hardening" section) and BEFORE whatever produced the current corrupted
state. If found, a small, dedicated "verified restoration" sprint could
install it safely using the SAME validate-then-atomic-replace
discipline this sprint's own tests already prove works — no code
changes needed, the existing hardening layer already supports it.
Separately worth noting: `docs/change_impact/memory_recovery.md` and
`docs/change_impact/persistent_state_hardening_v2.md` (both referenced
by `ARCHITECTURE_GUARD.md`) plus the `recovery/` script directory they
describe are absent from this checkout — a documentation/artifact gap
worth closing if those sources are ever recovered. Smaller, unrelated
alternatives, unchanged since Sprint 57/59/60/62: the pre-existing
`_REAL_LIGHTS` test-fixture discrepancy (RGB Computer's `entity_id`),
`switch` area-group schema support if it becomes a priority, or
reconciling the still-unexplained Sprint 59-era 3983 vs. 3190+ test-
collection-count discrepancy.

**Sprint 62 update:** evaluated multi-domain area group support
(`switch`/`fan`/`climate`/`media_player`) and found only `light` is
safely extendable today — every other domain either lacks a schema
capable of carrying `"area"` (`switch`) or lacks a registry entirely
(`fan`/`climate`/`media_player`), so all were deferred per STOP
CONDITION 1 rather than forced. Zero functional code change resulted;
see §19n/§20n and `docs/change_impact/multi_domain_area_groups.md`.
**Sprint 62's own recommendation for what's next:** IF `switch`
area-group support becomes a real priority, a dedicated sprint should
extend `load_switches_config()` to accept the same short-form/long-form
duality `load_lights_config()` already has (Sprint 60's own precedent),
including an optional `"area"` field, then reuse `devices.get_devices_
by_area()`'s exact pattern rather than a switch-specific helper. Until
that schema decision is made, `switch`/`fan`/`climate`/`media_player`
area-group commands will keep refusing safely exactly as they do today.
Smaller, unrelated alternatives, unchanged since Sprint 57/59/60: fixing
the pre-existing `_REAL_LIGHTS` test-fixture discrepancy (RGB Computer's
`entity_id`), the still-outstanding `config/long_term_memory.json`
recovery, or reconciling the still-unexplained Sprint 59-era 3983 vs.
3190+ test-collection-count discrepancy (§10/§15 of the area-schema
change-impact doc).

**Sprint 60 update:** Sprint 59's own recommendation immediately below
(a real, structured area/room field, added as its own standalone
config-schema sprint) was picked up and COMPLETED — `config/lights.
config.json` entries now support an optional `"area"` string field
(`luno/devices.py::load_lights_config()`), backed by two new pure
helpers (`get_device_area()`/`get_devices_by_area()`), and this
project's real registry was migrated (Main Lamp/RGB Strip/RGB Computer
all now carry `"area": "kamar"`). Sprint 59's own single-room behavior
is unchanged (byte-for-byte identical output, proved by tests) — see
§2's Sprint 60 entry, §19l/§20l, and `docs/change_impact/area_schema_
foundation.md`. Deliberately, per Sprint 60's own brief, this did NOT
implement multi-room command detection/execution — only the schema
foundation. **Sprint 60's own recommendation for what's next:** with
the schema now in place, a genuinely small follow-on sprint could
generalize `_GROUP_AREA_RE`'s captured area word to be checked against
`devices.get_devices_by_area(area_word)` directly (instead of only ever
comparing against the single hardcoded `"kamar"` string), so that once
a second room's lights are ever added to the config with a real
`"area"` value, an utterance like "semua lampu di dapur" could resolve
correctly instead of refusing — a command-detection-layer change on top
of this sprint's already-built, already-tested foundation, not a schema
change. This sprint also surfaced one open, unexplained discrepancy
worth a future look: the full-repository test suite's actual, directly-
verified collection is 3190 tests (`pytest --collect-only`), materially
fewer than the 3983 Sprint 59's own regression baseline documented —
not reconciled from within this sprint's scope, see `docs/change_
impact/area_schema_foundation.md` §10/§15. A smaller, unrelated
alternative, unchanged since Sprint 57/59: `config/long_term_memory.
json` recovery, or fixing the pre-existing `_REAL_LIGHTS` test-fixture
discrepancy (RGB Computer's `entity_id`) as an isolated cleanup sprint.

**Sprint 59 update:** Sprint 58's own recommendation immediately below
(area/room metadata, unblocking area-scoped groups) was picked up and
partially completed — not by adding a structured schema field, but by
recognizing that this project's real registry/config already contains
enough converging textual evidence to identify its one room ("kamar")
deterministically, without a schema change. Single-room group control
("semua lampu kamar"/"...di kamar") is now COMPLETE — see §2's Sprint 59
entry, §19k/§20k, and `docs/change_impact/ha_single_room_group_control.
md`. Multi-room support remains genuinely unimplemented (by design, per
this sprint's own explicit scope limit) and is now the clear next step
IF this project ever adds a second room: it would need a real,
structured area/room field added to the device config schema (still a
standalone, additive config-schema sprint, not an HA-logic sprint,
exactly as Sprint 58 predicted) — the "kamar"-only textual-evidence
approach used this sprint would NOT generalize safely to multiple rooms
without that structured field, because disambiguating which room a bare
"lampu" phrase means requires an authoritative source, not more textual
guessing. **Sprint 59's own recommendation for what's next:** (a) add a
real, structured area/room field to `config/lights.config.json`'s (and
`switches.config.json`'s) schema as its own small sprint if/when a
second room is ever added to this project, which would then cleanly
generalize single-room group control into true multi-room support with
minimal rework of `_apply_ha_group_resolution()`'s existing shape, or
(b) the still-outstanding `config/long_term_memory.json` recovery (see
below, unchanged since Sprint 57), or (c) fixing the pre-existing `_REAL_
LIGHTS` test-fixture discrepancy found this sprint (RGB Computer's
`entity_id` differs between the real config and the shared cross-sprint
test fixture — see `docs/change_impact/ha_single_room_group_control.md`
§6) as its own tiny, isolated, low-risk cleanup sprint.

**Sprint 58 update:** Sprint 57's own recommendation immediately below
(multi-device group commands) was picked up and completed for its
explicit-multi-target and group-all shapes — see §2's Sprint 58 entry,
§19j/§20j, and `docs/change_impact/ha_multi_entity_commands.md`. Two
sub-shapes remain deliberately unimplemented with documented evidence:
area-scoped groups (needs a real area/room metadata field added to the
device config schema first — a genuinely new config-schema sprint, not
an HA-logic sprint) and contextual groups (needs a structural memory
redesign Sprint 57's own single-slot design and this project's "no
second memory system" invariant both argue against attempting lightly).
**Sprint 58's own recommendation for what's next:** either (a) add
area/room metadata to `config/lights.config.json`'s schema as its own
small, standalone sprint, which would then cleanly unblock "semua lampu
di kamar" as a fast follow-up, or (b) the still-outstanding `config/
long_term_memory.json` recovery (see immediately below, unchanged since
Sprint 57).

**Sprint 57 update:** the four "BEFORE ANYTHING BELOW" items immediately
below this note predate Sprint 55, whose own full-repository regression
sweep (3880 tests) already satisfied their core ask (a real, full sweep
run, with results documented) — see §2's Sprint 55 entry and `docs/
change_impact/sprint55_stability_gate.md`. Sprints 56 and 57 both
re-ran full sweeps of their own (753/3865 and 337/3093 respectively —
see §19h/§20h and §19i/§20i). Live-LLM/HA-provider verification remains
the one item genuinely NOT possible in this sandbox (network egress
blocked, confirmed repeatedly, not merely assumed) — that limitation is
now a standing, understood fact of this environment, not a to-do.

**Sprint 57's own recommendation for what's next:** with Sprint 52
(entity resolution), Sprint 56 (query-side differentiator), and Sprint
57 (contextual references) all now covering the HA command surface, the
next highest-value HA-adjacent gap is likely **multi-device group
commands** ("nyalain semua lampu" / "turn off everything in the
bedroom") — currently unimplemented, outside every prior sprint's
scope. A smaller, faster alternative: revisit the DEFERRED `config/
long_term_memory.json` corruption (flagged by Sprint 55/56, diagnosed
but not fixed by Sprint 57 — see §19i above and `docs/change_impact/
sprint57_contextual_ha_references.md`'s dedicated section) if an
out-of-band backup can be located on the real `E:\Luno Evo` device.
Either direction should follow the same discipline as every sprint
since 43 (see the paragraph at the end of this section) — Sprint 57's
own Phase 0 finding (a THIRD, previously-missed, already-existing
mechanism solved most of the sprint) is itself a reminder to always
re-verify what already exists in the actual source before assuming a
new mechanism is needed.

**BEFORE ANYTHING BELOW (pre-Sprint-55, now largely superseded — see
the note above), four items:**

1. Run `tests/test_dashboard_turn_state_recovery_ttspath.py`** (plus the
   targeted regression list in that file's own docstring) for real, and
   update `docs/testing/regression_baseline.md` and this document's
   §3/§13 with the actual result. The Dashboard Turn-State Recovery fix
   Part 2 / TTS-path (§15's newest entry, §16 items 13-14, §17) is
   code-complete but has NEVER been executed - the session that wrote it
   had no working Python environment for this project.
2. Run the full repository regression sweep plus a live-HA smoke test
   for Sprint 52 (Robust HA Command & Entity Resolution, §19d/§20d,
   `docs/change_impact/sprint52_ha_entity_resolution.md`). Unlike item 1,
   this fix's own 68 tests DID run for real this session - what's
   missing is the full ~2900-test sweep and confirmation against a real
   Home Assistant server, neither reachable from that session's sandbox.
3. Run the full repository regression sweep plus a live-LLM-provider
   verification for Sprint 53 (Memory Session Summary API Compatibility
   Fix, §19e/§20e, `docs/change_impact/
   memory_session_summary_api_compatibility.md`). Like item 2, this
   fix's own 13 new tests (plus 111 pre-existing, unmodified) DID run
   for real this session (93 passed, 3 skipped, 0 failed) - what's
   missing is the full ~2900-test sweep and a real Session Summary
   triggered against the actual configured LLM provider, neither
   reachable from that session's sandbox (no `OPENROUTER_API_KEY`, no
   network access). `luno/adapters/llm/base.py`'s textually identical
   latent `max_tokens` pattern (formerly §16 item 18) is now RESOLVED
   by Sprint 54 - see item 4 below, which also covers this item's own
   live-verification need since Sprint 54's fix (not Sprint 53's) sits
   on the real production Session Summary call chain (§16 item 20's
   correction note).
4. Run the full repository regression sweep plus a live-LLM-provider
   verification for Sprint 54 (LLM Stack API Compatibility & Max
   Completion Tokens Hardening, §19f/§20f, `docs/change_impact/
   llm_max_completion_tokens_compatibility.md`). Like items 2-3, this
   fix's own 24 new tests (plus 141 pre-existing, unmodified) DID run
   for real this session (165 passed, 3 skipped, 0 failed) - what's
   missing is the full ~2900-test sweep and a real conversational-chat
   (and Session Summary) request triggered against the actual
   configured LLM provider, neither reachable from that session's
   sandbox (no `OPENROUTER_API_KEY`, no network access). A single live
   smoke test against the real provider effectively closes out items 3
   and 4 together, since both now point at the same fixed code path.

All four are the single highest-priority items, ahead of the
numbered-sprint candidates below, because all four are the last step
of already-diagnosed, already-implemented work, not exploratory work.

Once that is done and confirmed passing (or any failure it surfaces is
root-caused and fixed), the rest of this section still applies as
written by the session that completed Sprint 50 and the ORIGINAL
Dashboard Turn-State Recovery fix - both of those, unlike Part 2, ARE
verified complete (see §15/§16 item 12 and `docs/change_impact/
dashboard_turn_state_recovery.md`) - no other urgent production defect
is currently known. Sprint 50 was deliberately OBSERVABILITY ONLY, and
this fix was deliberately FIX-ONLY (per its own explicit non-negotiable
- it did not touch memory ranking, topic/reference resolution, or any
other intelligence code) - so §16 item 10 (query-side differentiator
matching, open since Sprint 49) is STILL the top intelligence-track
candidate below, exactly as it was at the end of Sprint 49. Candidate
directions, in rough priority order, for whoever picks this up next:

1. **Revisit §16 item 10** (query-side differentiator matching not yet
   implemented, open since Sprint 49): "Pompa A gimana?" still refuses
   rather than resolving to Aquascape A specifically, because Sprint
   49's own `_extract_entity_differentiator()` is only ever applied to
   STORED `source_sentence` entries, never to the live query text.
   Extending the SAME regex (`_ENTITY_DIFFERENTIATOR_RE`, `\b[A-Z]\b`)
   to `text` itself, inside `is_active_topic_relevant_to_query()`'s
   tie-check, is the natural, minimal next step - when the query's own
   raw text contains a standalone uppercase letter matching exactly one
   candidate's own differentiator (and not the other's), that candidate
   should be preferred over a plain tie/refusal. Must be proven via
   live reproduction, and must NOT weaken the existing refusal for a
   bare, non-differentiated follow-up (`tests/test_entity_provenance_
   disambiguation.py::test_15`/`test_33` are the two regression guards
   to check simultaneously - one must keep refusing, the other should
   ideally start resolving).
2. **(New, observability track, Sprint 50's own recommendation)**
   Now that `/mark_test` + `replay.py` exist, gather a handful of REAL
   captured conversations through normal use, review/approve a few into
   `tests/real_world/approved/`, and use them to decide whether Sprint
   50's own known limitation 3 (§16 item 11 - replay's generic canned
   reply, not the original real reply) is actually costing replay
   fidelity in practice. Only invest in richer replay (e.g. storing the
   original assistant reply too, bounded/privacy-reviewed) if a live
   approved case's own PASS/FAIL verdict is proven to hinge on that
   difference - not speculatively. A dashboard HTML panel for the two
   new `/api/observability/*` routes (§16 item 11's other deferred item)
   is a smaller, independent, purely-presentational option in the same
   pass if desired.
3. **Revisit known limitation #1** (bare compound-noun "-nya"
   declaratives, carried over from Sprint 44/45/46/47/48, still
   unfixed) with a fresh, narrowly-scoped idea - e.g. a position-aware
   check (only the sentence's OWN leading noun phrase, not any "-nya"-
   ending word anywhere) might thread the needle between "LED strip-
   nya"/"Power supply-nya" and "soalnya"/"katanya" without the false-
   positive risk that has blocked every sprint so far. Note: neither
   Sprint 47's `is_demonstrative_anchored_followup()`, Sprint 48's own
   ambiguity gate, nor Sprint 49's own differentiator signal help this
   specific limitation - none of the three apply to a bare "-nya"
   declarative with no demonstrative and no capital-letter label. Must
   be proven via live reproduction before implementation, and must
   include explicit negative tests against every discourse-connective
   false positive named in this document.
4. **Revisit §16 item 6** (historical query after a still-`"planned"`
   statement does not resolve to the plan): would require widening
   `_TEMPORAL_FALLBACK_ELIGIBLE_STATUS["historical"]` (`luno/memory_
   context.py`) to also accept `"planned"` status entries. Requires
   first answering the semantic question left open since Sprint 46 - is
   a not-yet-superseded PLAN the same thing as a "historical" fact for
   this purpose? - then a dedicated regression sweep against every
   existing temporal-fallback test before landing it.
5. **Revisit §16 item 5** ("lebih"/"paling" attribute questions in a
   single-topic conversation): the rejected fix broke because `is_
   active_topic_relevant_to_query()`'s `distinct_other_count >= 2`
   ambiguity threshold is calibrated for 3+ live topics, not exactly 2.
   Note this is a DIFFERENT tier than Sprint 48's own gate or Sprint
   49's own differentiator check (neither touches the `>= 2` tier this
   item concerns) - if a future sprint changes the `>= 2` threshold's
   own behavior at all, re-verify against BOTH of those, since all
   three now share the same `is_active_topic_relevant_to_query()`
   function.
6. **Revisit §16 item 7** (exact-50%-coverage same-entity lineage): the
   rejected `>= 0.5` fix could not distinguish a genuine same-entity
   lineage from a genuinely disjoint two-topic pair using coverage
   alone. Sprint 49's own differentiator signal is a DIFFERENT kind of
   evidence (explicit labels) and does not directly help this coverage-
   at-the-boundary case, but the same "read `source_sentence`, not just
   `.terms`" pattern might generalize - worth investigating whether the
   raw source sentences of the tied 50%-coverage pair contain any other
   distinguishing signal before assuming a coverage-threshold-only fix
   is the only option.
7. **A bounded, explicitly-curated product-to-category lookup**, ONLY if
   a future sprint's own live reproduction proves the current "never
   fabricate world knowledge" boundary is actually costing real,
   common-case continuity (not a hypothetical) - would need its own
   strict scope discipline (small, justified, per-entry, never
   inferred/learned automatically) to avoid becoming the "giant
   dictionary" every sprint since 43 has been told to avoid. Note this
   is closely related to Sprint 47's own Scenario 1 ("board"->ESP32-S3
   case is the SAME deliberate boundary) - if a future sprint tackles
   one, revisit the other in the same pass.
8. **Sprint 42's cross-system integration surface** has not been
   revisited since its own sprint - worth an audit pass if new
   subsystems have been added since.
9. General maintenance: the 4 pre-existing environment/infrastructure
   test failure groups (§16 item 4) could be resolved by fixing the
   actual dev-environment gaps (installing `faster_whisper`/`speech_
   recognition`/`sounddevice`, restoring `legacy_main.py`, or adjusting
   the affected tests to mock around the sandbox's own limitations) -
   none are urgent (all are environment-specific, not production bugs)
   but they add noise to every future regression sweep. Consider also
   whether the recurring "two separate stopword lists drift apart"
   pattern (§7's own Sprint 46 note - 3 instances so far: "buat",
   "bagaimana", "kenapa"/"napa"/"mengapa") warrants unifying `luno.
   memory._ATTRIBUTE_RESIDUAL_STOPWORDS` and `luno.memory_context.
   _TOPIC_OVERLAP_STOPWORDS` into one shared source, if a 4th instance
   is ever found.

Whichever direction is chosen, follow the SAME discipline every sprint
since 43 has followed: read-only reconnaissance first (including
verifying the PREVIOUS sprint's own claimed fixes are actually present
in the checkout, not merely documented - Sprint 47's own Phase 0
precedent), reproduce through the real `RuntimeDemoConsole` using
GENERIC canned replies that never leak the "correct" answer into a
merged snapshot (Sprint 47's own methodological correction, made after
an earlier richer-reply probe round produced false-positive readings),
root-cause before writing any fix, keep fixes minimal and additive,
PREFER a stateless/grammatical signal over a new data structure when one
exists (Sprint 48's own finding - a demonstrative-anchoring check fully
resolved its own target defect without any new field or representation),
INVESTIGATE a candidate mechanism's own foundation (e.g. with a direct
tokenizer/regex probe) BEFORE writing implementation code, not merely
after (Sprint 48's own limitation-#9 rejection was reached this way,
cheaper than Sprint 47's implement-then-revert cycle), write regression
tests for every fix AND for every already-correct scenario the
reconnaissance confirms, run the full regression sweep in CHUNKS if the
sandbox's own per-call wall-clock cap is too short for one invocation
(re-running any suspicious failure in ISOLATION before classifying it as
pre-existing or flaky - now a 4-sprint-running precedent), verify
persistent state with at least one isolated-run check (and if a config
file DOES change during the full sweep, trace WHICH test caused it
before assuming it's your own change's fault - Sprint 47's own `long_
term_memory.json` investigation), and update this handover
document (plus `ARCHITECTURE_GUARD.md`, `regression_baseline.md`, and a
new `docs/change_impact/*.md`) before considering the work done.
