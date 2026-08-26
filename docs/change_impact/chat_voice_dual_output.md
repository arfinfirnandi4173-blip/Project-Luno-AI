# Change Impact Analysis — Chat / Voice Dual Output

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md` per
`ARCHITECTURE_GUARD.md` §9 (this change touches a Protected Core
subsystem - `BehaviorTreeModule._speak()` and
`PlannerBridgeModule._handle_utterance()` in `main_runtime_demo.py`).

```
FEATURE:
Chat / Voice Dual Output - a presentation-adaptation layer so ONE LLM
response produces two independent presentation strings: chat_text (kept
essentially as-is - markdown/code/lists/technical detail preserved) and
voice_text (cleaned via the EXISTING normalize_for_speech(), then,
depth-aware, compressed for DETAILED replies only). No second LLM call,
no TTS streaming, no adaptive response-depth learning - all explicitly
out of scope.

WHY:
Luno's replies are currently sized/formatted for a text UI even when
spoken aloud - markdown/code/URLs/lists get run through
normalize_for_speech()'s existing sanitizer, but nothing adapts LENGTH
to the channel, and Chat/Voice have always been the SAME string with no
explicit separation. This sprint gives Chat and Voice their own explicit
presentation, reusing (never duplicating) the existing text normalizer
and the existing, already-shipped Response Depth Policy.

FILES TO CHANGE:
- luno/response_output.py (NEW - DualResponse dataclass +
  build_dual_response(); pure, self-contained, imports only
  luno.response_policy's depth constants/ResponsePolicy and
  luno.text_normalizer's normalize_for_speech()/rules)
- main_runtime_demo.py:
  - PlannerBridgeModule._handle_utterance(): one new additive publish
    (response_depth_assigned, correlated by request_id) immediately
    after the existing once-per-turn compute_response_policy() call -
    mirrors the EXISTING speaking_mode_assigned precedent exactly.
  - BehaviorTreeModule.__init__(): one new bounded instance attribute
    (_last_turn_depth), mirroring _last_turn_request_id.
  - BehaviorTreeModule._generate_reply(): one new ad-hoc
    event_bus.subscribe()/unsubscribe() pair for
    response_depth_assigned (same pattern already used for
    assistant_response/llm_error in this exact method), capturing the
    depth into self._last_turn_depth right where
    self._last_turn_request_id is already set.
  - BehaviorTreeModule._speak(): the ONE call site that previously
    called normalize_for_speech(text) directly now calls
    build_dual_response(text, depth, request_id=request_id) and
    publishes SpeakRequest with data["text"] = dual.voice_text (never
    dual.chat_text) plus two new informational fields
    (data["depth"], data["voice_adapted"]).
  - Removed the now-unused `from luno.text_normalizer import
    normalize_for_speech` top-level import (its only call site was
    replaced above; luno/response_output.py still imports and uses it
    internally).
  - Docstring/flow-diagram comments updated to reflect the new
    response_depth_assigned step and the dual-output _speak() step -
    no behavior in the comments-adjacent code changed beyond what's
    listed above.
- tests/test_response_output.py (NEW - 31 tests: 20 pure-function tests
  + 11 end-to-end tests through the real RuntimeDemoConsole pipeline)

DIRECTLY AFFECTED SUBSYSTEMS:
- Behavior Tree bridge / TTS trigger path (BehaviorTreeModule._speak() -
  what gets published as SpeakRequest.data["text"] changes from a bare
  normalize_for_speech() call to a DualResponse.voice_text; SpeakRequest
  gains two new, purely additive data fields)
- Planner bridge (PlannerBridgeModule._handle_utterance() gains one new
  event publish - no existing note/behavior in that method changed)

INDIRECTLY AFFECTED SUBSYSTEMS:
- Fish Audio Adapter (still only ever reads SpeakRequest.data["text"] -
  shape of that field's CONTENT changes (now depth-aware-compressed for
  DETAILED replies), but the event contract itself - type, correlation
  fields - is unchanged; FishAudioAdapter's own code was not touched)
- Console/Dashboard Chat display (assistant_response - the event both
  the console's conversation_log and the Dashboard Event Bus stream
  read from - is completely untouched; this sprint required ZERO
  changes there, since that path already carried the raw, undecorated
  text, which already satisfied "Chat stays detailed")
- Response Depth Policy (luno/response_policy.py) - NOT modified. This
  sprint only CONSUMES its already-resolved depth once per turn via the
  new response_depth_assigned event; compute_response_policy() itself,
  its scoring, and its existing system_prompt wiring are all unchanged
  (verified: exactly one compute_response_policy() call per turn, same
  as before this sprint - see Tests below).
- Memory / Relationship Engine / Emotion Engine - NOT touched.
  luno/response_output.py imports nothing from any of them (verified by
  direct source read, not a scan test, since the module's own import
  list is short enough to read exhaustively).

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- SpeakRequest event contract - additive only. data["text"] still means
  "literally what should be vocalized right now" (unchanged semantics,
  just now depth-aware-adapted text instead of a flat
  normalize_for_speech() pass); data["raw_text"] (the original reply,
  already present before this sprint) is unchanged; data["depth"]/
  data["voice_adapted"] are two NEW, optional, informational fields - no
  existing subscriber reads them, so nothing existing can break by their
  presence.
- AssistantResponse event contract - completely untouched, zero lines
  changed in any adapter that publishes it.
- Coordinator routing table - unchanged. response_depth_assigned reaches
  BehaviorTreeModule via that module's OWN ad-hoc
  event_bus.subscribe()/unsubscribe() inside _generate_reply() (the
  exact same mechanism already used for assistant_response/llm_error in
  that method) - no new Coordinator.add_route() call, no change to
  on_event()'s dispatch table.
- Response Depth Policy's own contract (ResponsePolicy/
  compute_response_policy()/build_depth_instruction()) - completely
  untouched.

EXPECTED REGRESSION RISKS:
- Low for the wiring itself: all edits to main_runtime_demo.py are
  additive (new lines/new event only) except the one changed line
  inside _speak() (normalize_for_speech(text) -> build_dual_response(...))
  and the removed now-dead import - both are exercised directly by the
  new end-to-end tests, which prove Chat is untouched and Voice receives
  adapted (never raw) text.
- Low-to-moderate for the voice-adaptation heuristics themselves (a
  brand-new deterministic algorithm) - mitigated by 20 pure-function
  tests covering every required artifact type (markdown/bullets/
  numbered lists/code/inline code/URLs/mixed) plus 4 dedicated semantic-
  safety tests proving warnings/numeric specs/conclusions/lead sentences
  survive DETAILED compression.
- A genuinely unrelated, PRE-EXISTING test-isolation bug was found (and
  fixed, test-side only) during this sprint's own regression sweep - see
  the Appendix below. It is unrelated to Chat/Voice Dual Output's own
  code paths (it is about a stray background thread from ANY real
  conversational turn, triggered by many other pre-existing tests too),
  but is documented here because this sprint's regression sweep is what
  surfaced it.

TESTS TO RUN:
- python3 -m pytest tests/test_response_output.py -q  (new, focused)
- python3 -m pytest tests/test_response_policy.py tests/test_runtime_demo.py
  tests/test_persistent_state_hardening.py tests/test_barge_in_console.py
  tests/test_wake_session_console.py tests/test_real_fish_audio_console.py
  tests/test_wake_barge_in_integration.py -q
  (speak()/TTS-trigger-path/depth-policy regression tests directly
  adjacent to what this sprint touches)
- python3 -m pytest luno/text_normalizer/tests/test_text_normalizer.py -q
  (this sprint's ONLY external dependency - proves it is unmodified)
- Full tests/ sweep (54 files, per-file/small-batch due to sandbox
  timeout constraints) + luno/ FAST suite

NEW TESTS REQUIRED:
- tests/test_response_output.py - SHORT/NORMAL/DETAILED behavior, empty
  response, multiline response, markdown emphasis, bullet list, numbered
  list, code block, inline code, URL, mixed markdown/code/URL, very long
  DETAILED response (compression + no crash), already-short response,
  Indonesian language, English language, string-depth acceptance,
  unknown-depth fallback, DualResponse field-naming guard, 4 semantic-
  safety tests (warning/numeric-spec/conclusion/lead-sentence survival
  under DETAILED compression), plus 11 end-to-end tests through the real
  RuntimeDemoConsole pipeline: Chat receives raw untouched text, Voice
  receives adapted text (never raw markdown), all three depths reach
  SpeakRequest.data["depth"], exactly one build_dual_response() call per
  turn, exactly one NeedLLMResponse per turn, and
  compute_response_policy() still called exactly once per turn (re-
  confirming test_response_policy.py's own guarantee still holds in this
  sprint's context).
- tests/test_state_isolation.py - 3 new tests for the unrelated fix (see
  Appendix): reproduces the straggler-thread race deterministically via
  an artificially slow mock LLM response, proves the new drain mechanism
  prevents real-file mutation, and a structural source-scan guard on the
  fixture's before-yield/after-yield ordering.

ROLLBACK PLAN:
Revert the edits in main_runtime_demo.py (restore the direct
normalize_for_speech(text) call in _speak(), remove the
response_depth_assigned publish/subscribe, remove _last_turn_depth,
restore the normalize_for_speech import) and delete
luno/response_output.py + tests/test_response_output.py. Nothing else in
the repository references luno/response_output.py (new module, no other
consumer). The tests/conftest.py straggler-thread fix (Appendix) is
independent and does not need to be rolled back alongside this feature -
it is a general test-safety improvement, not part of the dual-output
contract itself. No persistent state or config schema was touched by
either change, so no data migration/rollback is needed on that front.
```

## Appendix — unrelated test-isolation bug found during this sprint's regression sweep

**Not part of Chat/Voice Dual Output's own feature scope.** Documented
here because this sprint's own Phase 10 regression sweep is what
surfaced it (twice, empirically, via sha256/mtime diff on the real
file), and per this project's established convention (see e.g. the
Memory Conflict Resolution sprint's own account in
`docs/testing/regression_baseline.md`) of documenting bugs found and
fixed during a sprint's own test-writing/regression process.

**Root cause:** `PlannerBridgeModule.on_event()` (main_runtime_demo.py)
spawns a bare, untracked `threading.Thread(daemon=True,
name="luno-planner-turn")` per `user_utterance` - NOT submitted through
`Dispatcher`, so nothing in `Runtime.stop()`/`RuntimeDemoConsole.stop()`
(which only ever calls `dispatcher.stop(wait=False)`/
`event_bus.stop(wait=False)`) has any way to wait for it. Any test that
builds a real `RuntimeDemoConsole`, triggers a genuine conversational
turn, and calls `console.stop()` before that turn's own
`_handle_utterance()` naturally finishes can leave this thread running
past the test's own boundary. If that straggler's own
`RelationshipStore.save()` call (inside `_handle_utterance()`) happens
to execute AFTER `tests/conftest.py`'s per-test `config.RELATIONSHIP_STATE_FILE`
redirect has already reverted (monkeypatch's automatic revert, which
happens at THIS test's fixture teardown), the write lands on the REAL
`config/relationship_state.json` instead of the isolated path.

**Evidence:** caught live twice during this sprint's own regression
sweep - `config/relationship_state.json`'s real, legitimate accumulated
state (`interaction_count: 18`, a genuine recent timestamp) was found
overwritten with test-shaped values (`interaction_count: 1`-`2`,
`trust: 0.01`-`0.02`, the synthetic clock value `last_interaction_timestamp:
1000006.0`) at 2026-08-10 07:03:53 UTC and again at 16:11:14 WIB
(09:11:14 UTC) that same session. Both times, `luno/persistence.py`'s
mandatory backup-before-write meant the real prior state was fully
recoverable from `config/backups/relationship_state.<timestamp>.json` -
nothing was permanently lost. A live diagnostic trace (temporary prints
in `RelationshipStore.load()`/`save()`, reverted immediately after)
confirmed the exact mechanism via a captured stack trace showing
`RelationshipStore.save()` executing on thread `luno-planner-turn`, and
confirmed the race is genuinely intermittent (the same 26-file batch
that reproduced it reproduced it inconsistently across repeated runs,
consistent with a timing-dependent race, not a deterministic bug).

**Fix (test-isolation-only, zero production code changed):**
`tests/conftest.py`'s `isolate_persistent_state` fixture was converted
from a `return`-based to a `yield`-based fixture; immediately after
`yield paths`, it now calls a new `_drain_straggler_threads()` helper,
which joins any live thread named `"luno-planner-turn"` (bounded, 5s
total) BEFORE returning. Because pytest tears fixtures down in reverse
dependency order, and `isolate_persistent_state` depends on
`monkeypatch`, this drain runs BEFORE `monkeypatch`'s own automatic
attribute revert - so any straggler is forced to finish (and write)
while `config.RELATIONSHIP_STATE_FILE` still points at THIS test's
isolated path, never the real one. `_STRAGGLER_THREAD_NAMES` is
deliberately a small, explicit set (currently just
`"luno-planner-turn"`) rather than "join every new thread" - long-running
service threads (Event Bus pump, Heartbeat, Scheduler, Dispatcher pool
workers) are expected to keep running independently, and joining those
here would risk hanging a test rather than protecting one.

**Why this wasn't fixed in production code:** explicitly out of scope
per this investigation's own instructions - the fix must live entirely
in test infrastructure, not change `Runtime.stop()`/`Dispatcher.stop()`/
`PlannerBridgeModule.on_event()`'s real shutdown behavior. A production-
side fix (e.g. tracking/joining per-turn threads, or submitting them
through `Dispatcher` instead of a bare `threading.Thread`) would be a
more complete fix and is a reasonable candidate for a FUTURE, explicitly
scoped task - not performed opportunistically here.

**Regression proof:** `tests/test_state_isolation.py` gained 3 new
tests - `test_planner_turn_thread_can_genuinely_outlive_console_stop`
(proves the race is real, using an artificially slow mock LLM response
to make it deterministic rather than timing-luck-dependent),
`test_drain_straggler_threads_prevents_real_file_mutation` (proves the
fix works at the mechanism level - real file hash/mtime unchanged after
a forced straggler is drained), and
`test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
(a structural source-scan guard proving the drain call is positioned
after `yield`, not before - catches a future edit that might silently
move it to the wrong place). The exact batch that twice reproduced the
bug (26 files including `test_device_context.py`) was re-run 3
consecutive times after the fix with zero real-file mutation each time.

**Persistent-state verification performed:** `config/relationship_state.json`
was restored to its last known-good real state
(`config/backups/relationship_state.20260810T140353530750.json` -
`interaction_count: 18`, `trust: 0.02`,
`last_interaction_timestamp: 1786302610.445008`) once, after the fix was
verified working. All 6 other persistent JSON stores plus
`config/vision_memory.sqlite3` were sha256-hashed before this sprint's
Phase 2 baseline and re-checked after every subsequent test batch
throughout the sprint - byte-identical every time, no drift on any of
them at any point.
