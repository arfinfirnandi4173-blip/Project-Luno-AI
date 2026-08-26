# Change Impact: Conversation End Lifecycle Race Safety

## Summary

Closes the race documented as a known limitation by the Conversation_ended
Lifecycle Routing sprint (`ARCHITECTURE_GUARD.md` §21,
`docs/change_impact/conversation_ended_lifecycle_routing.md`'s own "Known
limitations"): `_handle_utterance()` runs its adaptive depth-feedback
update on a background thread, and if `conversation_ended` for the same
conversation is delivered and fully processed before that update runs,
the final adaptive-preference merge could read stale (pre-update) state
and silently miss the turn's feedback. This sprint adds one small,
purpose-built `threading.Condition` that makes conversation-end
lifecycle-safe with respect to in-flight turns, without touching
EventBus routing, response-depth policy, persistence schema, or any
other subsystem.

## The exact race window

`PlannerBridgeModule.on_event()` (`main_runtime_demo.py`) spawns a new
`threading.Thread(target=self._handle_utterance, ..., name="luno-planner-turn")`
per `user_utterance` event - fire-and-forget, no join, no tracking, prior
to this sprint. `_handle_utterance()` runs synchronously on that thread
and performs several non-zero-latency steps BEFORE it ever touches
adaptive depth-feedback state:

```
_handle_utterance(event):
    relevant_memories_early = memory_retriever.retrieve_memories(text)   # <- takes real time
    memory.record_memory_usage(...)
    emotion_tracker.observe(text)
    compute_response_policy(...)
    ...
    _handle_memory_feedback_command(...)
    _update_session_feedback_target(...)
    self._update_depth_preference(conversation_id, text)   # <- THIS writes _depth_preference[conv_id]
    ...
```

Independently, `SessionManagerModule` (`luno/wake_session/manager.py`)
publishes `ConversationEnded` on its own timer thread (inactivity timeout)
or in response to a manual-sleep command - with no awareness of, or
coordination with, any in-flight turn. Once routed to `"planner"` (§21),
this reaches `_on_conversation_ended()`, whose body (unchanged by this
sprint) reads `self._depth_preference.get(session_id)` and, if it has
accumulated feedback, merges it into the persisted baseline and pops the
conversation's local entry.

**The race:** if the Event Bus delivers and finishes processing
`conversation_ended` for `conversation_id` X during the window between
"turn X's thread starts" and "turn X's thread reaches
`_update_depth_preference()`", the merge in `_on_conversation_ended()`
runs against a `_depth_preference` dict that does not yet contain turn
X's contribution. The turn later finishes and writes an entry - but
`_depth_preference[X]` has already been popped and will never be read
again by a merge for that (now-closed) conversation. The feedback is not
corrupted, and no crash occurs - it is simply never persisted. This is
purely a probabilistic timing window: normal operation (a turn well
underway, or completed, before the conversation ends) never hits it, but
an inactivity-timeout firing shortly after a fast final utterance is
exactly the shape of interaction most likely to.

## Before/after lifecycle

**Before this sprint:**

```
user_utterance -> on_event() -> spawn thread, no tracking, no wait possible
                                        |
                                        v
                              _handle_utterance() running
                              (memory retrieval, emotion,
                               policy, ... still in progress)
                                        |
        conversation_ended (independent timer/manual) -----> _on_conversation_ended()
                                        |                            |
                                        |                    reads _depth_preference[X]
                                        |                    (may not have this turn's
                                        |                     contribution yet)
                                        |                            |
                                        |                    merges + pops + cleans up
                                        v                            |
                          _update_depth_preference() finally
                          runs, writes a NEW, orphaned
                          _depth_preference[X] entry that
                          will never be read again
```

**After this sprint:**

```
user_utterance -> on_event():
                     with _active_turn_lock:
                       if conversation_id in _ending_conversations: refuse, log, return
                       _active_turn_counts[conversation_id] += 1
                     spawn thread
                          |
                          v
                _handle_utterance() running ... try: _update_depth_preference() ...
                                                finally: _mark_turn_settled(conversation_id)
                                                         (decrements count, notifies waiters)
        conversation_ended -----> _on_conversation_ended():
                                     _wait_for_turn_to_settle(session_id):
                                       with _active_turn_cv:
                                         _ending_conversations.add(session_id)
                                         while _active_turn_counts.get(session_id, 0) > 0:
                                           wait(bounded by turn_settle_timeout_s)
                                         # on timeout: force-clear count, log, proceed anyway
                                     try:
                                       merge + pop  (UNCHANGED body, now sees the settled state)
                                     finally:
                                       _ending_conversations.discard(session_id)
```

The merge+cleanup body of `_on_conversation_ended()` itself was not
rewritten - only wrapped with a `_wait_for_turn_to_settle()` call before
it, and a `try/finally` around it to guarantee the "ending" mark is
always cleared.

## Why the synchronization mechanism is safe

- **Atomic check-and-increment, not check-then-spawn.** `on_event()`
  performs the "is this conversation already ending?" check and the
  "record one more in-flight turn" increment under the SAME lock, in one
  critical section. A non-atomic version (check, then separately
  increment) would leave a window where a new turn could sneak in
  between a waiter observing `count == 0` and that same waiter returning
  from `_wait_for_turn_to_settle()` - the atomic version closes that
  window entirely, not just narrows it.
- **One condition variable, one lock, per-conversation-keyed data.**
  `_active_turn_counts` and `_ending_conversations` are both plain dicts/
  sets keyed by `conversation_id`/`session_id` - there is no cross-
  conversation coupling anywhere in the new code. Waiting on conversation
  A's turn to settle only ever inspects and waits on A's own count;
  `notify_all()` wakes every waiter, but each waiter's `while` condition
  only re-checks its OWN conversation's count, so a wake for conversation
  B's settlement does not cause conversation A's wait to spuriously
  return early with stale state - it simply re-checks, finds its own
  condition still true or false as appropriate, and continues or exits
  correctly.
- **Separate lock from the persisted-baseline lock.** `_active_turn_lock`/
  `_active_turn_cv` is intentionally a different lock from the
  pre-existing `_persistent_depth_preference_lock` (which guards the
  read-merge-write sequence against the persisted JSON file). Conflating
  them would make the in-flight-turn wait hold the SAME lock the
  persistence merge needs, unnecessarily widening the critical section
  with no correctness benefit.
- **`finally` everywhere it matters.** `_mark_turn_settled()` is called
  from a `finally` block immediately after `_update_depth_preference()`'s
  own try/except, so the turn's "in-flight" count is decremented on
  every code path - success, a caught exception inside depth-preference
  update, or (since it's `finally`, not `except`) even if some future
  change added a code path that raises past that point. Likewise,
  `_ending_conversations.discard(session_id)` runs from a `finally`
  wrapping the merge+pop body, so a conversation is never left
  permanently marked "ending" (which would otherwise cause `on_event()`
  to refuse it forever) even if the merge itself raised.

## Bounded wait / timeout behavior

`_wait_for_turn_to_settle()` never waits indefinitely. It computes a
deadline (`time.monotonic() + self.turn_settle_timeout_s`, default
`2.0`s, configurable per-instance via the new `PlannerBridgeModule.
__init__(..., turn_settle_timeout_s=2.0)` parameter, same shape as this
class's pre-existing `tool_timeout_s`) and loops on
`condition.wait(timeout=remaining)`, re-checking the actual in-flight
count each time it wakes (guarding against spurious wakeups). If the
deadline passes while the count is still nonzero - a genuinely
hung/crashed worker thread that never reaches `_mark_turn_settled()` -
the count entry is force-cleared, a log line records the conversation id
and configured timeout, and processing proceeds using whatever state is
already recorded. `_on_conversation_ended()` is therefore GUARANTEED to
return within roughly `turn_settle_timeout_s` of being called, regardless
of what the in-flight turn's thread is doing. Verified by
`test_case_C_worker_hang_times_out_without_deadlock_or_corruption`
(`turn_settle_timeout_s=0.3`, the blocked turn is never released, and the
test asserts both bounded completion time and a correctly force-cleared
count).

## Duplicate-event behavior

A second `conversation_ended` for the same `session_id` - whether it
arrives after ordinary completion or after a force-timeout - is still a
safe no-op, unchanged from §21's guarantee. `_wait_for_turn_to_settle()`
re-adds the id to `_ending_conversations` and immediately finds the count
at `0` (nothing in flight, since the first call already cleared or
consumed it), so it returns without waiting. The merge-gating condition
downstream (`ended_preference is not None and ended_preference.
feedback_count > 0`) and every `.pop(key, None)` call are exactly as
before - a repeat call finds nothing left to merge or pop. Verified by
`test_case_D_duplicate_conversation_ended_after_hang_timeout_is_idempotent`
(explicitly measures that the second call returns quickly, not by
re-waiting) and `test_cleanup_occurs_exactly_once_per_conversation_ended_event`.

## Concurrency / cross-conversation isolation behavior

Every new structure is keyed by `conversation_id`/`session_id`; there is
no global lock, no cross-conversation blocking, and no shared mutable
state that isn't partitioned by key. Verified two ways: (1)
`test_case_E_concurrent_conversation_isolation_during_a_wait` holds
conversation A's turn deliberately open (via a scoped, text-matched mock
of `memory_retriever.retrieve_memories`) and runs a full, unrelated
conversation B turn to completion while A is actively inside its bounded
wait, asserting B is never added to `_ending_conversations` and completes
normally; (2)
`test_no_global_lock_regression_unrelated_conversations_proceed_during_a_wait`
runs FIVE unrelated conversations' turns to completion while conversation
A waits, asserting they all complete well under the wait's own timeout
budget - if the new lock behaved like a runtime-wide lock, these would
queue up behind A's wait instead. `test_no_cross_conversation_state_leak_across_many_interleaved_races`
additionally ends four conversations concurrently (via real threads, real
published events) and asserts no conversation's local adaptive-preference
entry is ever touched by another's cleanup.

## Adaptive preference implications

This is the practical payoff: a turn's depth-feedback contribution that
would previously have been silently dropped by fast-timeout-after-
fast-turn timing is now reliably captured. Verified end-to-end, through
the real Event Bus, in both directions:
`test_short_preference_survives_the_race_and_seeds_the_next_process` and
`test_detailed_preference_survives_the_race_and_seeds_the_next_process`
each deliberately hold a turn's memory-retrieval step open, publish
`conversation_ended` while it is still blocked, release it, and confirm
(a) the persisted file appears with the correct-direction bias and (b) a
brand-new process/console loaded from that file applies the correct
adaptive modifier, in the correct direction, to its very first turn. The
merge itself remains exactly as conservative as §20 established
(`PERSIST_BLEND_WEIGHT = 0.3`, untouched) - this sprint changes WHEN the
merge is allowed to run, never HOW it computes its result. Explicit user
instructions still unconditionally override any persisted baseline in
both directions after the race window closes
(`test_explicit_short_overrides_persisted_detailed_after_race_window`/
`test_explicit_detailed_overrides_persisted_short_after_race_window`) -
inherited, unchanged, from `compute_response_policy()`.

## EventBus path (reconfirmed unchanged)

The route added by §21 - `conversation_ended -> EventBus -> "planner" ->
PlannerBridgeModule.on_event() -> _on_conversation_ended()` - was neither
touched nor duplicated by this sprint.
`test_route_table_still_contains_conversation_ended_to_planner_exactly_once`
and `test_repeated_bind_event_bus_still_does_not_duplicate_the_route`
reconfirm both route tables and idempotent `bind_event_bus()` behavior
are exactly as §21 left them.

## Race reproduction (concrete evidence)

`test_race_reproduction_zero_wait_loses_the_late_turns_feedback` runs the
SAME production code path (`RuntimeDemoConsole`, real Event Bus,
`_on_conversation_ended()` reached only via a published event) with
`turn_settle_timeout_s=0`, which collapses `_wait_for_turn_to_settle()`
to "check once, don't actually wait" - a faithful stand-in for this
project's literal pre-sprint behavior, since no synchronization of any
kind existed before this sprint. The test deliberately holds a turn open
past the point `conversation_ended` is published and processed, then
confirms: no persisted file exists immediately after the (near-instant)
`conversation_ended` processing completes; the held turn is then
released and allowed to finish, writing its own (now-orphaned)
`_depth_preference` entry; and - the defining symptom of the race - no
persisted file EVER appears, because the one merge opportunity for that
conversation has already passed. `test_B_case_new_ordering_waits_and_
captures_the_late_feedback` runs the IDENTICAL scenario at the real
default `turn_settle_timeout_s=2.0` and proves the fix: `_on_
conversation_ended()` blocks (confirmed still actively waiting -
`conv_id in _ending_conversations`, no file yet - before the turn is
released) until the turn settles, and the file appears with the
correct-direction bias immediately after release. These two tests share
every line of scenario setup and differ only in `turn_settle_timeout_s` -
the cleanest available before/after comparison on the exact same code
path.

## Known limitations

- `turn_settle_timeout_s`'s default (`2.0`s) is a judgment call, not a
  value derived from a formal analysis of realistic turn-processing
  latency distributions - it mirrors this class's pre-existing
  `tool_timeout_s` in shape and order of magnitude. A conversation whose
  final turn's memory retrieval, LLM call, or tool execution genuinely
  takes longer than this to reach `_update_depth_preference()` will still
  have its feedback silently dropped on a real, if now much smaller,
  timing window - the sprint's fix bounds the WAIT, it does not make the
  race impossible in the presence of an arbitrarily slow turn. This is
  the same conservative posture `tool_timeout_s` already accepts
  elsewhere in this class.
- The pre-existing, unmodified `_on_conversation_ended()` merge-gating
  condition (`ended_preference.feedback_count > 0`) does not distinguish
  "this conversation's local bias was already flushed to the persisted
  baseline by the primary per-turn `%3` trigger" from "this conversation
  still has genuinely new, unflushed evidence" - ending a conversation
  immediately after its local bias already crossed that primary
  threshold performs one additional (still correctly-blended,
  `PERSIST_BLEND_WEIGHT`-conservative) merge of the same local bias into
  the already-updated baseline. This is a pre-existing characteristic of
  the merge-gating logic itself (predates this sprint, unrelated to the
  race this sprint closes) - documented here because
  `test_case_A_turn_finishes_before_conversation_ended_still_works` had
  to account for it explicitly rather than assume a no-op. Changing this
  gating condition was out of scope (hard constraint: "do not change
  adaptive feedback semantics unless required to close the race" - it is
  not required).
- A conversation that reuses a `session_id` immediately after that same
  id finished ending (§21's pre-existing behavior, reconfirmed
  unaffected by `test_case_F_immediate_new_conversation_with_same_id_is_
  not_blocked_forever`) is accepted as soon as `_on_conversation_ended()`
  has passed its merge+pop section - there is no cooldown or grace
  period. This was already true before this sprint and was not changed.
