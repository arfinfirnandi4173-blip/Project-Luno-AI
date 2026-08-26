# Change Impact: Conversation_ended Lifecycle Routing

## Summary

Fixes a previously-documented, out-of-scope gap (`ARCHITECTURE_GUARD.md`
§15, `CURRENT_STATE.md`'s Known Limitations, and
`docs/change_impact/persistent_adaptive_response_depth.md`'s Known
Limitations): `conversation_ended` events were never routed to the
`"planner"` module, so `PlannerBridgeModule._on_conversation_ended()`
(`main_runtime_demo.py`) was only ever reachable via a direct, white-box
test call - never through the real production Event Bus. This sprint
adds the missing route in both places it needed to exist. No handler
logic changed.

## Root cause

`PlannerBridgeModule.on_event()` (`main_runtime_demo.py`) has always
contained a correct dispatch branch:

```python
elif event.type == "conversation_ended":
    self._on_conversation_ended(event)
```

The bug was never in this handler, and never in `_on_conversation_ended()`
itself (its body already correctly merges the persistent adaptive-depth
preference before popping local state, and every pop already tolerates a
missing key). The bug was that nothing ever told the `Coordinator` to
forward `conversation_ended` events to the module registered under the
name `"planner"` (`PlannerBridgeModule.name = "planner"`,
`main_runtime_demo.py`).

`Coordinator.add_route(event_pattern, module_name)`
(`luno/core/coordinator.py`) is, by this project's own architecture rule
("nothing in this codebase should ever call another subsystem's method
directly"), the ONLY mechanism by which a published event reaches a
module's `on_event()`. An event type with no registered route silently
reaches nobody - no error, no exception, just silence. This project
maintains TWO separate route tables that must be kept in sync (documented
as a deliberate "byte-for-byte mirror" in `luno/bootstrap/modules.py`'s
own module docstring, not shared code - a small amount of intentional
duplication over cross-file coupling, the same tradeoff `luno.wake_session`/
`luno.barge_in` already made for a different reason):

1. `main_runtime_demo.py`'s `RuntimeDemoConsole.__init__` - used by the
   developer console (`python main_runtime_demo.py`) AND by every test
   in this project that loads `main_runtime_demo.py` via
   `importlib.util.spec_from_file_location()`.
2. `luno/bootstrap/modules.py`'s `register_all_modules()` - used by real
   production (`python main.py`).

**Both tables were missing the route.** Table 1 had no `conversation_ended`
route of any kind. Table 2 already routed `conversation_ended` to
`"proactive"` (a pre-existing, unrelated route feeding `ProactiveModule`'s
out-of-cycle goal evaluation - human_entered/human_left/person_appeared/
planner_finished/wake_word_detected/conversation_ended all trigger an
immediate evaluation rather than waiting for the next tick) but not to
`"planner"`.

## Why the fix is minimal

The fix is exactly two lines, one per route table, each placed
immediately after the pre-existing `add_route("user_utterance", "planner")`
line it mirrors:

```python
runtime.add_route("conversation_ended", "planner")             # luno/bootstrap/modules.py
self.runtime.add_route("conversation_ended", "planner")        # main_runtime_demo.py (RuntimeDemoConsole.__init__)
```

No other file changed its behavior. Specifically NOT touched, because
the Phase 0 audit found no reason to touch them:

- `PlannerBridgeModule.on_event()` / `_on_conversation_ended()` - already
  correct.
- `DepthPreference`, `compute_response_policy()`, `detect_depth_feedback()` -
  unrelated to routing.
- `luno/response_depth_preference.py`'s persistence schema/thresholds -
  unrelated to routing.
- TTS, streaming, barge-in, memory retrieval - unrelated subsystems; the
  Phase 0 audit traced every `conversation_ended` reference in the
  repository and found no coupling between this routing fix and any of
  them.
- `Coordinator`/`EventBus` themselves - the routing mechanism (fan-out,
  `add_route()` semantics) already supports exactly what this fix needed
  (multiple targets per event pattern); nothing about the mechanism was
  incomplete or needed extending.

## Before/after event flow

**Before:**

```
SessionManagerModule (luno/wake_session/manager.py)
  publishes ConversationEnded (type="conversation_ended")
      |
      v
EventBus - delivers to every module with a matching Coordinator.add_route()
      |
      v
  (production: "proactive" only)      (console/tests: nobody)
      |
      X   <-- chain breaks here. PlannerBridgeModule.on_event() is never
              called with this event type. _on_conversation_ended() is
              unreachable except via a direct test call.
```

**After:**

```
SessionManagerModule (luno/wake_session/manager.py)
  publishes ConversationEnded (type="conversation_ended")
      |
      v
EventBus - delivers to every module with a matching Coordinator.add_route()
      |
      +----------------------------+
      v                            v
  "proactive" (unchanged)      "planner" (NEW route)
                                    |
                                    v
                          PlannerBridgeModule.on_event()
                                    |
                                    v
                       _on_conversation_ended(event)
                                    |
                    persistent adaptive-preference merge
                    (if this conversation ever produced
                     real depth feedback)
                                    |
                                    v
                    conversation-local state cleared
                    (_depth_preference, _response_depth_context,
                     _last_response_policy, _last_device_target,
                     _session_feedback_target, _session_feedback_context,
                     _last_turn_trace, _pending_env_confirmations,
                     decision_engine.affinity)
                                    |
                                    v
                     session summary archived (if a real
                     session_summary_client is wired in)
```

## Ordering guarantees

Verified unchanged (no fix needed - the pre-existing code was already
correct): within `_on_conversation_ended()`, the persistent adaptive-depth
merge (`ended_preference = self._depth_preference.get(session_id)` ...
`merge_conversation_into_persistent(...)` ... `DepthPreferenceStore.save(...)`)
runs BEFORE `self._depth_preference.pop(session_id, None)`. Read-then-pop,
never pop-then-read - so the merge always sees the conversation's real,
final local bias, never a value that was already discarded.

**Pre-existing, out-of-scope limitation this sprint did NOT fix:**
`_handle_utterance()` (which calls `_update_depth_preference()`) runs on
its own background thread per turn. `SessionManagerModule`'s
inactivity-timeout publish of `ConversationEnded` happens independently,
on its own timer thread. If the very last turn's
`_update_depth_preference()` has not yet completed by the time
`conversation_ended` is delivered, that turn's feedback may not be
captured in the final merge. This is not data corruption
(`_persistent_depth_preference_lock` still guarantees the shared
in-memory value and on-disk file are never corrupted regardless of
ordering) - only a possible missed contribution to one merge event, on
an already-conservative signal that self-corrects over many future
conversations. Fixing this would mean redesigning turn/timeout
synchronization, explicitly out of scope for a routing-only sprint.

## Conversation isolation

Unchanged - every piece of state `_on_conversation_ended()` touches is
keyed by `session_id`/`conversation_id`, never global. Ending
conversation A only ever touches dict entries keyed under A's id.
Verified end-to-end (via a REAL published event, not a direct call) by
`tests/test_conversation_ended_lifecycle_routing.py::test_F_ending_one_conversation_does_not_touch_another_active_one` -
conversation B's local adaptive preference is asserted byte-identical
before and after A's real `conversation_ended` event is published and
processed.

## Duplicate-event behavior

A second `conversation_ended` for a `session_id` that was already
cleaned up is a safe no-op:

- Every conversation-scoped dict pop uses `.pop(session_id, None)` -
  tolerates a missing key.
- `self.decision_engine.affinity.reset(session_id)` is documented as a
  no-op for an unknown key.
- The persistence merge is gated on `ended_preference is not None and
  ended_preference.feedback_count > 0`; after the first call already
  popped `_depth_preference[session_id]`, `self._depth_preference.get(session_id)`
  returns `None` on the second call, so the merge block is skipped
  entirely - no double-merge, no double-write.

Verified by `test_C_duplicate_conversation_ended_event_is_safe`
(publishes the event twice for the same `session_id`, asserts the
on-disk persisted preference is unchanged between the two),
`test_D_unknown_conversation_id_is_safe` (a `session_id` that was never
used), and `test_D2_empty_conversation_id_is_safe` (`session_id=""` and
a payload with no `session_id` key at all - the Event Bus pump thread
must not crash, and the console must keep routing normally afterward).

## Adaptive preference persistence implications

This is the practical payoff of the fix: the SECONDARY best-effort final
merge in `_on_conversation_ended()` (added by the Persistent Adaptive
Response Depth Preference sprint, previously documented as unreachable
in production) is now reachable through the real Event Bus. A
conversation that produces 1 or 2 depth-feedback events - never crossing
the PRIMARY per-turn `should_persist()` %3 threshold - no longer loses
that evidence entirely when the conversation ends; it is now flushed to
the persisted baseline via the real `conversation_ended` path, not only
when a test calls `_on_conversation_ended()` directly. Verified
end-to-end, through the real Event Bus, in both directions:
`test_G_H_short_direction_real_event_persists_and_seeds_next_process`
and `test_G_H_detailed_direction_real_event_persists_and_seeds_next_process`
(each: 2 feedback events, below threshold, real `conversation_ended`
publish, assert the persisted file appears with the correctly-blended
bias, then a brand-new process/console loads it and applies it, in the
correct direction, to a brand-new conversation's very first turn).

A single conversation's merge remains deliberately conservative
(`PERSIST_BLEND_WEIGHT = 0.3`, unchanged) - it will not, by itself, flip
a borderline-NORMAL query into the SHORT/DETAILED bucket. This is
correct, by design (see
`docs/change_impact/persistent_adaptive_response_depth.md`'s "why it
isn't persisted every turn" section) - this sprint's E2E tests assert
the CORRECT DIRECTION and MAGNITUDE of the applied modifier (via
`_last_response_policy[conversation_id]["reasons"]`/`["score"]`), not an
unrealistic full bucket flip from a single lightly-fed-back conversation.

Explicit user instructions still unconditionally override any persisted
adaptive baseline, in both directions - unchanged, structural guarantee
inherited from `compute_response_policy()` (verified again here,
end-to-end through the real event-routed lifecycle, by `test_I`/`test_J`).

## Known limitations

- The turn/timeout race described under "Ordering guarantees" above -
  pre-existing, not introduced or fixed by this sprint, out of scope.
- `ProactiveModule`'s own handling of `conversation_ended` (immediate
  out-of-cycle goal evaluation) was not audited beyond confirming its
  existing route is untouched - it was already working before this
  sprint and remains outside this sprint's scope.
- This fix was verified end-to-end through `main_runtime_demo.py`'s
  `RuntimeDemoConsole` (what the test suite and developer console use)
  and via direct source inspection of `luno/bootstrap/modules.py` (real
  production). Spinning up the FULL real production bootstrap stack
  (real/mocked adapters, dashboard, health checks, `main.py`'s actual
  process) end-to-end was out of scope for this sprint's test suite -
  `tests/test_production_launcher.py`'s existing 23/24 passing suite
  (which already exercises `register_all_modules()`) was re-run and
  confirmed unaffected (see `docs/testing/regression_baseline.md`).

## Tests

`tests/test_conversation_ended_lifecycle_routing.py` (15 scenarios, new
file): route-table structural checks (route exists exactly once in the
console; the same line exists in `luno/bootstrap/modules.py`'s source;
repeated `bind_event_bus()` calls don't duplicate the route), real
Event-Bus reachability, exactly-once execution, duplicate-event safety,
unknown/empty conversation ID safety, full conversation-local cleanup
coverage, cross-conversation isolation (via a real published event), a
full short-direction and detailed-direction adaptive-preference E2E
(real event end -> persisted merge -> brand-new process seeds from it),
explicit-instruction override in both directions after a real
event-routed conversation end, and a persistent-state isolation sanity
check. No pre-existing test in `tests/test_persistent_adaptive_response_depth.py`
(whose `test_e2e_6`/`test_e2e_7` intentionally still call
`_on_conversation_ended()` directly, white-box, to test that method's
own logic in isolation from routing) was modified.
