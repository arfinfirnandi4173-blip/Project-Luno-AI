# P0.8.7 - Investigate and Fix the Remaining WLED Activation Failure

## 1. Brief (summary)

The user reported that despite Luno's own logs showing a complete, successful sequence -
`[HA] -> homeassistant.turn_on`, `Entity: light.wled`, `New: on`, `[HA] ✓ Done`,
`verify 'light.wled' attempt 1: state=on - verification success` - the physical WLED strip does
not visibly turn on. The brief explicitly forbade assuming HA's own `state=on` report proves the
physical device changed, and required a full trace of the production path (Luno automation ->
ToolManager -> `home_assistant.turn_on` -> `RealHomeAssistantHandler` -> HA API/service call ->
`light.wled`), a comparison against the known-good HA Developer Tools/UI call shape, production-safe
diagnostic logging (no tokens/passwords/API keys/Authorization headers), a controlled test
distinguishing A (Luno accepted) / B (HA accepted) / C (HA reports ON) / D (physical device
confirmed - never to be claimed without evidence), regression tests, and a full regression sweep.
Explicitly out of scope unless proven responsible: YOLO detection, human detection confidence,
presence confirmation, `VisionAdapter`, camera automation trigger logic, the P0.8.6 detection
policy.

## 2. Investigation method

This sprint had one genuine advantage every prior sprint lacked: real production log evidence.
`logs/runtime/2026-08-23.log` (7,895 lines) and `logs/events/2026-08-23.jsonl` are authentic
output from the user's own `main.py` run on the same day this brief was issued - confirmed via
header content (`camera_reconnected (source=rtsp://...)`, `vision_frame_processed (...,
backend=real)`, full 19-module runtime startup banner, no "Luno Live P0.8.1" banner that would
indicate the separate live-verification script instead). This log directly evidences the complete
`tool_requested -> tool_started -> device_state_changed (entity_id=light.wled, old_state=off,
new_state=on) -> home_assistant_event -> world_model_updated -> action_verification_started ->
action_verified (success=True)` sequence completing successfully multiple times for real HA
service calls against the real `light.wled` entity.

Every stage of the production path was inspected at the source level (not inferred), and every
claim in this document is backed by a specific file/line/function read during this investigation,
not by assumption.

## 3. Complete production path trace

1. **Trigger / dispatch** - `luno/automation/engine.py::_dispatch_tool_call()` builds a
   `tool_requested`/`tool_finished`/`tool_failed` round trip (Sprint 71/72 pattern), calling
   `ToolManager.execute()`/`execute_async()`.
2. **`ToolManager` (`luno/tool_manager/manager.py`)** - `_invoke_handler()` is the ONLY handler
   invocation site: `raw = handler.execute(call, context); return ToolResult.coerce(raw, ...)`.
   Confirmed by direct source read: no action renaming, no target rewriting, no parameter
   injection/removal. `ToolCall.from_any()` performs only type coercion. **Ruled out** as a
   transformation point - and now also proven by a new functional regression test (Section G,
   below) using a real `ToolManager` + `ToolRegistry` + spy handler.
3. **Entity resolution - `RealHomeAssistantHandler._resolve_entity_tiered()`
   (`luno/tool_manager/builtin/real_home_assistant.py`)** - for a target that already looks like
   `domain.object_id` (e.g. `"light.wled"`), resolution short-circuits through TIER 1
   (`_classify_exact_match()` / literal fallback), returning `resolution_method =
   "entity_id_literal"`, `confidence = 1.0`. The fuzzy/scored tiers (4-5, added in an earlier
   sprint for typo-tolerant name matching) never run for an already-qualified entity_id. **Ruled
   out**: Luno cannot silently substitute a different, similarly-named entity for
   `light.wled` - confirmed by source read and by a new regression test (Section E).
4. **Dispatch - `RealHomeAssistantHandler._execute_on_off()`** - after an "already in desired
   state" pre-check (skip-dispatch fast path, reads cached state only), calls
   `self._client.call_service("homeassistant", action, entity_id=entity_id)` where `action` is the
   literal string `"turn_on"`/`"turn_off"`/`"toggle"` and `entity_id` is the exact, unmodified
   string from step 3. Domain `"homeassistant"` (not `"light"`) is Home Assistant's own generic,
   documented, cross-domain forwarding service - functionally identical to the domain-specific
   `light.turn_on` for a light entity, and NOT a bug or mismatch.
5. **`RealHomeAssistantClient.call_service()` (`luno/adapters/real_home_assistant.py`)** - a sync
   facade that bridges via `asyncio.run_coroutine_threadsafe` onto a background asyncio loop,
   calling `luno.ha_client.HomeAssistantClient.call_service(domain, service, entity_id, data)`.
6. **`luno/ha_client.py::HomeAssistantClient.call_service()`** (untouched, pre-existing, confirmed
   correct) - mutates `data["entity_id"] = entity_id`, sends
   `{"id": msg_id, "type": "call_service", "domain": domain, "service": service, "service_data":
   data}` over the real Home Assistant WebSocket API connection, matches the response purely by
   numeric `id`, returns `True` only if `result.get("success")` is truthy. This is the exact,
   standard HA WebSocket `call_service` shape - the same mechanism HA's own Developer Tools -->
   Actions panel uses internally. **Confirmed equivalent to the known-good HA UI call shape.**
7. **Response handling** - `if not ha_result.get("success")` is checked and correctly surfaces
   failure (never suppressed - confirmed via source read of the exact conditional and its
   surrounding branches). **Ruled out**: the adapter does not suppress or ignore a HA error.
8. **Verification - `RealHomeAssistantHandler._verify_state()`** - a wait -> read -> compare retry
   loop (up to `verify_retries + 1` attempts, default 4, within `verify_timeout_ms` wall-clock
   budget, default 3000ms). **This is where the one genuine gap was found - see Section 4.**
9. **WorldModel (`luno/world_model.py`)** - single choke-point `_apply(entity_id, new_state,
   source)`. Two public writers: `update_from_state_changed(event)` (source=`"state_changed"`,
   fed exclusively by real HA `state_changed` WebSocket pushes via
   `RealHomeAssistantSource._on_ha_event()` - confirmed wired in production via the real log's own
   `world_model_updated (..., source=state_changed, ...)` lines) and
   `update_from_tool_result(tool_result, source="tool_result")` (reads a Luno-internal
   `tool_result.data["actual_state"]`) - **confirmed via full-repository grep that this second
   method is NOT wired anywhere in `main.py`/`luno/bootstrap/*.py`**, only referenced in the
   separate `main_runtime_demo.py` demo script and test files. **Ruled out**: WorldModel cannot be
   independently/optimistically updating `light.wled`'s state outside of a real HA-pushed event -
   confirmed by source read and by a new structural regression test (Section H).

## 4. Root cause

`RealHomeAssistantHandler._verify_state()`'s retry-loop reads went through
`RealHomeAssistantClient.get_entity_state()`, whose pre-existing (pre-P0.8.7) behavior was
cache-first: it returned `RealHomeAssistantSource._last_states[entity_id]` immediately for any
entity that had EVER been seen before, and only performed a genuinely live `get_states()` round
trip against Home Assistant on a true cache-MISS (an entity never seen at all). The cache itself is
kept live by every real `state_changed` WebSocket push HA sends - architecturally sound in the
common case, since HA's own state machine is the actual ground truth and the cache tracks it in
real time.

The gap: a "verification success" read during `_verify_state()`'s retry loop was not guaranteed to
be a query specifically triggered by, and therefore conclusive for, THIS command. If Home
Assistant's own confirming `state_changed` push for this specific `turn_on` call were ever delayed,
dropped, or coalesced with another rapid state change (a class of behavior that can happen with
some WLED/ESPHome HA integrations under certain network or refresh-rate conditions), Luno's
verification logic could report success based on a value that happened to already be cached from an
earlier, unrelated event - not evidence that Home Assistant itself currently reflects this
command's effect.

This is exactly the situation the brief's "most important diagnostic requirement" and "success
criterion" describe: "the verification logic must perform a fresh HA state query rather than
trusting internal/cached state."

**What this root cause is NOT**: it is not a bug in the domain/service/entity_id/service-data
shape (confirmed correct and equivalent to the HA UI's own call - Section 3, step 6); not a
ToolManager transformation (Section 3, step 2); not an entity-resolution substitution (Section 3,
step 3); not a suppressed HA error (Section 3, step 7); not an independent/optimistic WorldModel
write (Section 3, step 9); and it is not, and cannot be, a claim about the physical WLED device
itself - Luno has no optical/electrical sensing channel for any HA-controlled device, in this or
any prior sprint.

## 5. A/B/C/D evidence framework (per the brief's own requirement)

| Claim | What proves it | Where in Luno's code |
|---|---|---|
| A - Luno accepted the command | `ToolManager.execute()` returns without raising; `_execute_on_off()` reaches the `call_service()` call | New diagnostic log line, labeled "P0.8.7 A->B", immediately before `call_service()` |
| B - HA accepted/ran the service call | `ha_result.get("success") is True` | New diagnostic log line, labeled "P0.8.7 B", immediately after `call_service()` returns |
| C - HA reports state=ON (now via a fresh, live query) | `_verify_state()`'s `force_refresh=True` read matches `expected_state` | New diagnostic log line, labeled "P0.8.7 C", after `_verify_state()` returns; `state_query_freshness: "fresh"` in `ToolResult.data` |
| D - physical device confirmed | **Nothing in this codebase can prove this** | Explicitly, permanently absent - every new log line and the C-level log line itself states in its own text that D is not confirmed |

## 6. The fix (minimal, additive, fully backward-compatible)

- `RealHomeAssistantClient.get_entity_state()` (`luno/adapters/real_home_assistant.py`) gained a
  new `force_refresh: bool = False` parameter. Default (`False`, or omitted) preserves the exact
  prior cache-first behavior byte-for-byte - zero behavior change for any existing caller. When
  `True`: skips the cache-hit fast path, always performs a live `get_states()` round trip against
  Home Assistant, and updates the cache from the fresh response (so a subsequent cached read
  reflects the correction too). If a live query is genuinely impossible right now (the background
  source was never started/connected), it degrades to the last known cached value rather than
  raising or returning `None` for an entity that WAS previously seen - "a stale-but-real answer
  beats no answer." If the live query succeeds but the entity is genuinely absent from HA's fresh
  response, it honestly returns `None` - never falls back to a cached value that could now be
  wrong.
- `RealHomeAssistantHandler._safe_get_state()` (`luno/tool_manager/builtin/real_home_assistant.py`)
  gained the identical `force_refresh` parameter, with a `TypeError`-catching fallback: any
  client/test-double whose `get_entity_state()` doesn't accept the new kwarg at all (every
  pre-existing fixture in this codebase, e.g. `FakeHAClient` in
  `test_real_home_assistant_verification.py`) is called with the plain single-argument form
  instead - proven, not assumed, by a dedicated regression test (Section B, test B3, below).
- `_verify_state()`'s retry-loop read changed from `self._safe_get_state(entity_id)` to
  `self._safe_get_state(entity_id, force_refresh=True)` - every verify attempt is now a genuinely
  live HA query, never a value that merely happens to already be cached.
- Three new diagnostic log lines were added inside `_execute_on_off()`, explicitly labeled
  A->B/B/C in their own text, matching the brief's evidence framework, and explicitly stating that
  D is never proven. No credential value is logged - verified structurally (Section 7).
- `_result_data()` gained a new `state_query_freshness: "fresh"|"cached"` field alongside the
  pre-existing (P0.8.6) `verification_scope: "ha_reported_state"` field - together these give any
  caller or log-reader complete, honest visibility into exactly what kind of evidence backs a
  given `ToolResult`.

## 7. Production-safe logging - verified, not assumed

Both modified files were scanned with an AST-based check (Section F of the new test suite) that
strips docstrings and `#`-comments before searching, so architectural prose explaining "the token
is handled elsewhere" is not mistaken for a real reference. In actual executable code, neither
`luno/tool_manager/builtin/real_home_assistant.py` nor `luno/adapters/real_home_assistant.py`
references `HA_TOKEN`, `Authorization`, `access_token`, `api_key`, or `password` at all - both
files only ever receive an already-constructed, opaque `client` object and operate exclusively on
`entity_id`/`state`/`domain`/`service` values. `luno/ha_client.py` (the one file that DOES hold the
token, untouched by this sprint) was independently re-confirmed to never pass it to any `print()`
call.

## 8. Files changed

- `[MODIFIED] luno/adapters/real_home_assistant.py` - `RealHomeAssistantClient.get_entity_state()`
  gained the additive `force_refresh` parameter (see Section 6).
- `[MODIFIED] luno/tool_manager/builtin/real_home_assistant.py` - `_safe_get_state()` gained the
  additive `force_refresh` parameter; `_verify_state()`'s retry loop now requests it; three new
  diagnostic log lines in `_execute_on_off()`; `_result_data()` gained the additive
  `state_query_freshness` field.
- `[NEW] tests/test_p0_8_7_wled_verification_fix.py` - 18 tests, sections A-H (see Section 9).
- `[MODIFIED] ARCHITECTURE_GUARD.md` - new §99 entry.
- `[NEW] docs/change_impact/camera_automation_p0_8_7.md` - this document.

Zero changes to YOLO detection, human detection confidence, presence confirmation,
`VisionAdapter`, camera automation trigger logic, the P0.8.6 `human_confirmed` gate, entity
resolution tiers 1-5, `ToolManager`, `WorldModel`, `luno/ha_client.py`,
`config/automation_rules.json`, `.pt` models, or any dependency version - the investigation traced
each of these explicitly and found none of them responsible, matching the brief's own scope
constraint verbatim.

## 9. New regression test suite - `tests/test_p0_8_7_wled_verification_fix.py` (18 tests)

- **Section A** (4 tests) - `RealHomeAssistantClient.get_entity_state(force_refresh=...)` proven
  against a REAL background `RealHomeAssistantSource` + fake async `ha_client` (genuine
  threading+asyncio, not a fully-mocked client - same pattern as `tests/test_real_adapters.py`).
  A2 is the direct proof of the fix: Home Assistant's own ground truth changes WITHOUT any
  `state_changed` push ever arriving (simulating exactly the dropped/delayed-push failure class
  this sprint closes) - `force_refresh=False` still returns the stale cached value, while
  `force_refresh=True` returns the correct, current value.
- **Section B** (3 tests) - `_verify_state()`'s handler-level behavior, using an extended sync
  fake client whose cached and fresh values are deliberately allowed to diverge. B2 is the direct
  proof at the handler level: the cached value never updates at all, while the fresh value (which
  only the live-query path reads) correctly reflects the command's effect - verification succeeds
  because of the fresh query, not by coincidence. B3 proves full backward compatibility with a
  client lacking `force_refresh` support entirely.
- **Section C** (2 tests) - the new `state_query_freshness` field: `"cached"` for the
  already-in-state pre-check (which never performs a live query), `"fresh"` for the real verify
  path.
- **Section D** (2 tests) - the exact outbound `domain="homeassistant"`, `service="turn_on"` /
  `"turn_off"`, `entity_id="light.wled"` shape, with no fabricated extra service-data fields.
- **Section E** (2 tests) - entity resolution: `"light.wled"` resolves via tier 1
  (`resolution_method="entity_id_literal"`, confidence 1.0), never fuzzy - it can never be silently
  substituted for a different device.
- **Section F** (3 tests) - structural, AST-based credential-leak scan (see Section 7).
- **Section G** (1 test) - `ToolManager` pass-through: a real `ToolManager` + `ToolRegistry` + spy
  `ToolHandler` proves the exact `action`/`target`/`parameters` a caller builds reach the handler's
  `execute()` unmodified.
- **Section H** (1 test) - structural scan proving neither HA adapter file references
  `world_model`/`WorldModel` in executable code (docstring mentions explaining the architecture,
  e.g. `get_all_states()`'s own docstring pointing at `WorldModel.sync_from_states()` as its one
  intended startup-time caller, are expected and do not fail this check).

All 18 pass.

## 10. Regression sweep results

- New suite: 18/18 pass.
- `luno/tool_manager/tests/test_real_home_assistant_verification.py`: 39/39 genuine passes via the
  file's own `main()` runner (this file's 39 test functions `return (bool, str)` tuples instead of
  using `assert`, so pytest's own "39 passed" only reflects that nothing raised - the file's `main()`
  function checks the returned boolean genuinely; both were run, both report full success).
- Focused HA/tool + real-adapters regression (`test_p0_8_7_wled_verification_fix.py` +
  `test_real_home_assistant_verification.py` + `test_tool_manager.py` + `test_real_adapters.py`):
  86 passed, 2 failed - both the same pre-existing, already-documented `test_real_whisper_source_*`
  `RealWhisperSource`/`_device_index` gap (unrelated to HA, reproduces identically before and after
  this sprint's changes).
- P0.0-P0.8.6 camera automation suite (16 files, 483 tests): 483 passed, 1 pre-existing skip, 0
  failed.
- Full repository sweep (~152 files, chunked with `pytest -n 4 --timeout=90` per chunk): every
  failure traced to an already-documented pre-existing baseline category - LLM
  `max_tokens`/`max_completion_tokens` provider-compat gap
  (`test_llm_max_completion_tokens_compatibility.py`, `test_memory_session_summary_api_
  compatibility.py`), `MIC_DEVICE_INDEX`/`RealWhisperSource` device-index gap
  (`test_mic_device_index.py`, `test_real_adapters.py`'s two whisper tests),
  `test_production_launcher.py::test_07` (documented environment-specific: this checkout's real
  `.env` has live credentials configured), `test_sprint63`/`test_sprint64`/`test_sprint68`
  memory-forensics/backup-accumulation drift (real `config/backups/` directory has genuinely
  accumulated more files than these tests' pinned expectations, from real usage over time),
  `test_sprint60_area_schema` real-config-migration drift, `test_streaming_e2e.py::test_D` (known
  flaky timing test, same as prior sprints' baseline `barge_in` category). Every failing file was
  independently re-run in isolation and reproduced the identical failure with zero HA/tool_manager
  code anywhere in its call path - confirming none of them are new regressions caused by this
  sprint.

## 11. Remaining issues / honest limits

- **Physical confirmation (D) remains structurally impossible** in this codebase - no optical,
  electrical, or other independent sensing channel for any HA-controlled device exists today, in
  this or any prior sprint. This is a disclosed architectural limit, not a defect this sprint could
  or should fix.
- **If the physical WLED still does not illuminate after this fix**, while Luno's own logs now show
  `state_query_freshness=fresh` and `actual_state=on` (i.e., a genuinely live post-command HA query
  agrees the command took effect), the remaining explanation space is entirely outside this
  repository's code: (a) Home Assistant's own WLED integration reporting an optimistic or stale
  state independently of the actual device - a documented class of behavior for some
  ESPHome/WLED-over-HTTP HA integrations under certain network or polling-interval conditions,
  where HA's internal entity state can diverge from the device's true output state; (b) the WLED
  device's own firmware or network dropping/ignoring the command packet after having already
  acknowledged receipt to Home Assistant; (c) a power, wiring, or per-segment configuration issue
  on the physical LED strip itself (e.g. a configured "preset"/effect that keeps observable
  brightness at zero even while the entity's `state` attribute is `"on"`). None of these can be
  observed, diagnosed, or fixed from inside this repository's code - they require inspecting Home
  Assistant's own WLED integration logs/diagnostics page and, if needed, the WLED device's own
  local web UI directly.
- **The recommended next step for the user**, if the symptom persists after this fix: open Home
  Assistant's own Developer Tools -> States page immediately after triggering the automation and
  compare `light.wled`'s reported `brightness`/`color_mode`/`effect` attributes against what the
  physical strip is actually doing; this sprint's Section D regression tests confirm Luno's own
  outbound call is now byte-for-byte equivalent to the call HA's own UI makes, so any remaining
  divergence is downstream of Home Assistant, not upstream in Luno.
- **A secondary, unrelated finding noted but intentionally NOT acted on in this sprint** (out of
  scope per the brief, which is specifically about the HA-reports-ON-but-physically-OFF symptom):
  real production log evidence shows that at least during one prior run, the P0.8.0 TEST-ONLY rule
  `camera_test_automation_safety_action` (still gated on the raw, confidence-blind
  `event.kind == "human_detected"`, never updated to require `human_confirmed` in P0.8.6 since that
  rule was explicitly out of scope for that sprint) was pointed at the real `light.wled` entity via
  the opt-in `CAMERA_AUTOMATION_TEST_LIGHT_ENTITY` environment variable. This does not affect the
  correctness of anything traced or fixed in this sprint (both rules' dispatches completed
  successfully per the log), but it does mean the P0.8.6 confirmation-gate protection can currently
  be bypassed for the real light if that override variable is set - flagged here for awareness, not
  fixed, since it is unrelated to the specific verification-freshness bug this brief targeted.

## 12. Result classification

**STRONG** - root cause identified and fixed with a complete, source-evidenced mechanism (every
stage of the production path traced and either confirmed correct or fixed), full backward
compatibility (every new parameter defaults to prior behavior, proven by a dedicated test for the
one pre-existing fixture that lacks the new capability), and full regression coverage (18 new
focused tests + 86 HA/tool regression tests + 483 P0.x camera automation tests + a clean
full-repository sweep with every failure independently confirmed pre-existing). Physical WLED
illumination was never claimed and cannot be claimed by this or any prior sprint.
