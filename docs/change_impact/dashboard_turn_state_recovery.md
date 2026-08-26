# Dashboard Turn-State Recovery Fix

Status: COMPLETE. Fix-first task (not a numbered intelligence sprint) -
completed after Sprint 50 (Runtime Observability), before Sprint 51
(Query-side entity differentiator matching).

## Root cause / motivation

Reported symptom: the Luno Dashboard sometimes stays permanently in
`Thinking` state after a request/connection failure, and no further
command can be sent. Two Windows errors were named in the report:
`ConnectionAbortedError: [WinError 10053]` and `OSError: [WinError
10038] An operation was attempted on something that is not a socket`.

### Phase 0 reconnaissance findings

- `SessionManagerModule` (`luno/wake_session/manager.py`) owns the
  authoritative turn state (`ConversationSession.state`, a
  `ConversationState` enum - `SLEEPING/AWAKENING/LISTENING/THINKING/
  SPEAKING/WAITING_USER/IDLE`). `_forward_to_conversation()` sets
  `THINKING` the moment a genuinely-awake utterance is forwarded.
  `THINKING` has NO timeout anywhere in this codebase - confirmed by
  that file's own pre-existing docstring, which documents a PRIOR,
  already-fixed instance of exactly this class of bug (a fully-streamed
  reply never publishing `speak_request`, described there as "a
  PERMANENT session deadlock").
- The only existing recovery path for a failed/cancelled turn is
  `SessionManagerModule._handle_llm_failure()`, subscribed to
  `llm_error`/`llm_cancelled` only. Both are published exclusively from
  inside `OpenRouterAdapter._run_request()`
  (`luno/adapters/openrouter.py`), which already wraps the real LLM
  HTTP call in a broad `except Exception` and always publishes
  `LLMError` on failure - this specific path was already correct.
- The Dashboard's own busy-guard, `send_chat_message()`
  (`luno/dashboard/controls.py`), reads
  `session_manager.status_snapshot()['state']` directly (the real
  backend state, not a frontend cache) and refuses any non-barge-in-
  relevant text while `state in ("thinking", "speaking")`.
- `PlannerBridgeModule._handle_utterance()` (`main_runtime_demo.py`,
  ~1700 lines) is dispatched onto a freshly-spawned, unsupervised
  `luno-planner-turn` daemon thread (`on_event()`'s own
  `threading.Thread(target=self._handle_utterance, ...).start()`, no
  wrapper). That method already wraps ~23 of its own individually risky
  steps in their own `try/except` ("a bug here must never break a
  turn" - memory retrieval, tool execution, adaptive-depth feedback,
  session-summary persistence, ...), but large stretches between those
  blocks were NOT individually guarded - `self.planner.create_plan()`
  and the final `self._event_bus.publish(NeedLLMResponse(...))` call
  (literally the method's own last line) among them.
- Consequence: if any exception escapes `_handle_utterance()` BEFORE it
  ever reaches `NeedLLMResponse`, the OpenRouter adapter never starts a
  request, so `llm_error`/`llm_cancelled`/`assistant_response` never
  fire, `THINKING` never clears (no timeout exists), and the Dashboard's
  busy-guard rejects every further ordinary command forever. Python's
  default behavior for an uncaught exception on a bespoke thread is to
  print to stderr via `threading.excepthook` and let the thread die -
  nothing downstream is ever told the turn ended.
- Separately, `luno/dashboard/server.py`'s `_dispatch_get`/
  `_dispatch_post` already caught `ConnectionError` (covering
  `BrokenPipeError`/`ConnectionAbortedError`/`ConnectionRefusedError`/
  `ConnectionResetError` - the WinError-10053 shape) as an expected,
  silent client disconnect. `WinError 10038` ("not a socket") does NOT
  subclass `ConnectionError` (per PEP 3151, only `WSAECONNABORTED`/
  `WSAECONNRESET`/`WSAECONNREFUSED` map to a `ConnectionError`
  subclass; `WSAENOTSOCK` stays a plain `OSError`) - it fell into the
  generic `except Exception` branch, producing an unnecessary one-line
  log and a doomed attempt to write a 500 response back to a socket
  that, per the error itself, is no longer a socket.
- `luno/bootstrap/shutdown.py`'s `ShutdownCoordinator.shutdown()` calls
  `self.dashboard.stop()` first (step 2b, before cancelling in-flight
  LLM requests), and `DashboardServer.stop()` already wraps its own
  `self._httpd.shutdown()`/`server_close()` in `try/except Exception`,
  logging and continuing on any failure - a `WinError 10038` there was
  ALREADY caught, logged once, and non-fatal before this fix.

### Phase 1 reproduction - what was proven live, not assumed

Built a minimal, real-runtime reproduction directly against
`RuntimeDemoConsole` (mocked LLM/TTS clients, real Event Bus, real
`SessionManagerModule`/`PlannerBridgeModule`/`BargeInModule`/
`DashboardServer`):

- **Scenario A (normal turn):** reaches `THINKING`, returns to a
  non-busy state on its own. Baseline, unaffected by this fix.
- **Scenario B (Dashboard client disconnect):** an abrupt raw-socket
  close mid-`/api/events/stream` never affected backend session state;
  the server stayed fully responsive and `send_chat_message()` kept
  working immediately afterward. **Already correct before this fix** -
  Category C for the Dashboard's own connection-abort handling
  specifically (see classification below).
- **Scenario C (mid-turn exception):** `self.planner.create_plan()`
  monkeypatched to raise `ConnectionAbortedError("[WinError 10053] ...")`
  - the exact shape from the report. Before the fix: session state
  stayed at `thinking` indefinitely (verified never recovering within an
  8s window, and by inspection has no timeout to ever recover on its
  own), a real uncaught-thread-exception was captured via
  `threading.excepthook`, and `send_chat_message()` returned `{'ok':
  False, 'message': 'Luno is busy right now (state=thinking) - try
  again in a moment'}` afterward. **This is the confirmed root cause.**
- **Scenario D (cancellation):** `CancelLLMRequest` already correctly
  resulted in `llm_cancelled` -> `THINKING` clearing to `waiting_user`
  within the wait window. **Already correct before this fix.**
- **Scenario E (repeated recovery):** normal -> failure -> normal ->
  failure -> normal, `send_chat_message()` usable after every cycle.
  Exercised directly against the fixed code (see Phase 1 test file).

### Classification of the two named errors

| Error | Origin traced to | Classification |
|---|---|---|
| `ConnectionAbortedError [WinError 10053]` | Any exception surfacing inside `_handle_utterance()` before `NeedLLMResponse` publishes (proven via `self.planner.create_plan()`); a real device/network-backed tool call is the most plausible real-world trigger. Also possible, separately, as an ordinary Dashboard browser-tab disconnect mid-SSE-stream. | **A - root cause** (the `_handle_utterance()` case). The Dashboard-SSE-disconnect case was already Category **C - unrelated, already-handled noise** (proven in Scenario B). |
| `OSError [WinError 10038] "not a socket"` | The Dashboard's own per-connection socket after an earlier failed write on the same connection (a well-known Windows `socketserver`/`http.server` quirk, most likely to surface from the long-lived SSE keepalive write); also possible during interpreter/process shutdown teardown. | **B - secondary, already-harmless cleanup symptom.** Never corrupted shared runtime/session state (per-connection thread, contained by existing `try/except` layers with a `finally` that always unregisters the SSE subscriber); during `shutdown()` it was already caught, logged once, and non-fatal. Reclassified from "silently absorbed by a too-broad `except Exception`" to "explicitly recognized as an expected disconnect" (see fix below) purely to remove log noise and a doomed write - not because it was ever actually breaking anything. |

Verdict: **D - a combination**, but not the combination a naive read of
the traceback text would suggest. The 10053/10038 pair is NOT one
single failure cascading through the Dashboard's HTTP layer; it is two
independent, unrelated phenomena that happened to be observed close
together - a genuine turn-lifecycle defect (A) and ordinary,
already-mostly-handled connection noise (B/C). This is exactly why
Phase 0's own instruction ("do not classify based on the traceback text
alone") mattered here.

## The fix

### 1. `PlannerBridgeModule._run_utterance_turn_safely()` (new,
   `main_runtime_demo.py`) - the primary fix

`on_event()`'s `threading.Thread(...)` now targets this new wrapper
instead of `_handle_utterance()` directly. The wrapper calls
`self._handle_utterance(event)` inside `try/except` and, on any escaped
exception, publishes the SAME `llm_error` event a real OpenRouter
failure already publishes:

```python
self._event_bus.publish(Event(type="llm_error", data={
    "request_id": request_id,
    "conversation_id": conversation_id,
    "model": None,
    "error": str(ex),
    "error_type": type(ex).__name__,
    "retryable": False,
    "source": "planner_bridge_unhandled_exception",
}))
```

This reuses EXISTING routes and EXISTING, already-idempotent consumers
- zero new event type, zero new route, zero new state machine:

- `session_manager` (already routed to `llm_error` in both
  `RuntimeDemoConsole` and `luno/bootstrap/modules.py`) -
  `_handle_llm_failure()` clears `THINKING` back to `waiting_user`/
  `idle`, guarded by `if self.session.state != THINKING: return` (safe
  even against a hypothetical redundant publish).
- `barge_in` (already routed to `llm_error`) - `BargeInModule` already
  treats `llm_error` exactly like `llm_cancelled`: "this turn is over,
  clear my own busy flag too" (`luno/barge_in/manager.py`'s own `elif t
  in ("llm_error", "llm_cancelled")` branch) - this fix transitively
  also fixes `BargeInModule`'s own busy-state tracking for the same bug
  class, at no extra cost.

The `source: "planner_bridge_unhandled_exception"` field is the one new
piece of information added - it distinguishes "the turn never even
reached the LLM" from a real OpenRouter/network failure (which has no
`source` field), making the failure diagnosable from the Sprint 50
observability log/dashboard without any new event type.

A turn that completes normally is entirely unaffected - the wrapper's
own `try` simply returns once `_handle_utterance()` does.

### 2. `_is_expected_client_disconnect()` (new,
   `luno/dashboard/server.py`) - secondary connection-error hygiene

```python
def _is_expected_client_disconnect(ex: BaseException) -> bool:
    if isinstance(ex, ConnectionError):
        return True
    if isinstance(ex, OSError):
        return getattr(ex, "winerror", None) == 10038 or ex.errno == 10038
    return False
```

`_dispatch_get`/`_dispatch_post` gained a new `except OSError` branch
(after the existing `except ConnectionError`, before the generic
`except Exception`) that silently absorbs this specific shape and logs/
responds normally for every other `OSError` (a real bug must never be
silently hidden). This does not change any behavior a client can
observe - it only removes a redundant log line and a doomed
response-write attempt for a connection that is, per the OS itself,
already gone.

### 3. `luno/bootstrap/shutdown.py` - investigated, left UNCHANGED

`dashboard.stop()` already wraps its own socket teardown in
`try/except Exception` and logs+continues on failure. A live test
(`test_09_dashboard_stop_mid_turn_does_not_raise_or_corrupt_session_state`)
confirms `dashboard.stop()` completes without raising and without
corrupting `session_manager`'s state even while a real turn is
`THINKING`. No code changed here, per the brief's own "only modify if
proven related" instruction - it never was.

## Observability

No new event type needed. The reused `llm_error` event (with the new
`source` field) already flows through Sprint 50's `EventLogWriter`
(subscribes to `"*"`) and the dashboard's generic Event Bus page with
zero additional wiring. `turn started` (`user_utterance`/
`speech_recognized`), `turn completed` (`assistant_response`), `turn
failed` (`llm_error` - now covering BOTH a real LLM/network failure and
an unhandled `_handle_utterance()` exception, distinguished by
`source`), `turn cancelled` (`llm_cancelled`), and `connection aborted`
(now silently classified rather than mis-logged, in
`luno/dashboard/server.py`) are all already distinguishable from the
existing event stream.

## Tests

`tests/test_dashboard_turn_state_recovery.py` (new, 13 tests, all
E2E through the real runtime path - no test targets only a private
helper function):

1. normal turn returns to a non-busy state (baseline, unaffected)
2. exception in `create_plan()` recovers instead of sticking (the core
   fix, exact WinError-10053 shape)
2b. the uncaught-thread-exception no longer escapes (`threading.excepthook`
   stays empty)
3. Dashboard client disconnect mid-SSE-stream does not stick (proves
   Category C for the Dashboard's own connection layer)
4. cancellation returns to a non-busy state
5. repeated failure/recovery cycle (normal->failure->normal->failure->
   normal, the brief's own Scenario E) stays usable throughout
6. `send_chat_message()` actively rejects while stuck, then accepts
   immediately after recovery
7. `_is_expected_client_disconnect()` unit coverage - never misclassifies
   a real `OSError`
8. an expected WinError-10038 disconnect produces no error log line
9. `dashboard.stop()` mid-turn does not raise or corrupt session state
10. the reused `llm_error` event records `source=
    planner_bridge_unhandled_exception` for observability
11. `_handle_llm_failure()` is idempotent against a redundant `llm_error`
12. rapid sequential turns (busy-gate + this fix's recovery, composed)
    end in a clean, usable state

All 13 pass consistently in isolation and as part of the full suite.
A small amount of inherent timing variance exists under heavy sandbox
CPU load (multiple sequential real turns, real daemon threads, real
Event Bus dispatch) - generous, documented timeouts (up to 12s to reach
`THINKING` under full-suite load) were chosen after observing actual
sandbox behavior, not guessed defensively; this is the same category of
environment-driven timing sensitivity already documented for this
project's `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_
chunk_never_plays`.

## Regression

Targeted: `tests/test_dashboard_turn_state_recovery.py` (13),
`tests/test_dashboard.py` (47), `tests/test_runtime_demo.py` +
`tests/test_wake_barge_in_integration.py` (104) - all passing, 0
failures.

Full repository sweep (`tests/` - 100 files, 98 collectible, 2
pre-existing uncollectible - run in the established 8-chunk `pytest -n
4` methodology): **2960 collected, 2949 passed, 11 failed.** 10 of the
11 failures are byte-identical to the standing baseline
(`test_mic_device_index.py` x6, `test_production_launcher.py::test_07`,
`test_real_adapters.py` x2, `test_state_isolation.py`'s own documented
`inspect.getsource` sandbox gap). The 11th,
`test_verification_dashboard.py::test_api_verification_reports_a_
successful_verified_action_end_to_end`, failed with the EXACT SAME
`inspect.getsource`/`OSError: could not get source code` signature as
the already-documented `test_state_isolation.py` flake - re-run in
isolation, it passed cleanly, confirming a parallel-execution timing/
sandbox artifact (a new manifestation of an existing, already-documented
category of flake) rather than a regression from this fix. **Zero new
regressions.**

## Performance

`_is_expected_client_disconnect()` (20,000-call measurement across
three exception shapes): mean 0.0002ms/call. The
`_run_utterance_turn_safely()` try/except wrapper overhead on the
success path (100,000-call microbenchmark of the pattern in isolation):
mean 0.00017ms/call. Both far under the 5ms/turn target; no network
calls, no LLM calls, no embeddings, no disk I/O added to any hot path.

## Persistent state

`config/*.json` (15 files) SHA256-hashed before/after: the full
8-chunk repository sweep was byte-identical; two additional independent
isolated runs (this fix's own new test file alone; that file plus
`test_dashboard.py`/`test_runtime_demo.py`/
`test_wake_barge_in_integration.py`) were also both byte-identical. No
new `config/*.json` key, no new persistence path - this fix only
changes in-memory Event Bus/thread-lifecycle behavior.

## Known limitations

- The fix addresses exceptions escaping `_handle_utterance()` itself.
  If a FUTURE bug were introduced inside one of that method's own
  ALREADY-individually-guarded `try/except` blocks (e.g. a bug in
  `self.planner.execute()`'s own exception handler), it would still be
  contained by that block, not this new outer wrapper - this fix adds a
  single outermost safety net, it does not replace or duplicate the
  method's own existing internal discipline.
- `_is_expected_client_disconnect()`'s `WinError 10038` check is
  deliberately narrow (exact errno/winerror match only) - a different,
  not-yet-observed Windows socket errno in this same "already
  disconnected" family would still fall through to the generic
  `except Exception` handler (logged, not silently hidden) rather than
  being misclassified either way. Widening it further should only
  happen after a NEW concrete errno is actually observed, not
  speculatively.
- The reused `llm_error` event's `retryable: False` and `model: None`
  fields are honest placeholders for the "never even reached the LLM"
  case - a consumer that specifically branches on `model` for a real
  LLM failure should already be tolerant of `None` (the pre-existing
  `luno/dashboard/static/index.html`'s own `llm_error` handler only
  reads `d.error`, confirmed unaffected).

## Invariants preserved

No change to memory ranking, topic ranking, entity/reference
resolution, the semantic alias system, `ActiveTopicSnapshot`, the LLM
prompt architecture, TTS architecture, streaming architecture,
persistent conversation storage, any Sprint 45-50 ambiguity boundary,
Sprint 50's replay semantics, or `EventLogWriter`'s redaction/bounding
guarantees. `PlannerBridgeModule._handle_utterance()`'s own body and
every one of its existing internal `try/except` blocks are byte-for-
byte unchanged - only the THREAD TARGET calling it changed.

## Next recommended sprint

Sprint 51 - Query-side entity differentiator matching (Sprint 49's own
still-open recommendation; this fix-first task deliberately did not
touch intelligence/ranking code, per its own explicit non-negotiable).
