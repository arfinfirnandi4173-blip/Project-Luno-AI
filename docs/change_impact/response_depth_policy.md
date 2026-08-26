# Change Impact Analysis — Response Depth Policy

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md` per
`ARCHITECTURE_GUARD.md` §9 (this change touches a Protected Core
subsystem - the `PlannerBridgeModule._handle_utterance()` prompt-
assembly path in `main_runtime_demo.py`).

```
FEATURE:
Response Depth Policy - a deterministic, explainable policy deciding
whether Luno's reply to a given user turn should be SHORT/NORMAL/
DETAILED, computed once per turn and surfaced to the LLM via one small
additive prompt note. No Chat-vs-Voice dual output, no TTS chunking, no
adaptive learning - those are explicitly out of scope for this sprint.

WHY:
Luno currently produces responses sized for a text-chat UI even though
replies will eventually be delivered through voice, where unnecessary
length is a much bigger cost. This sprint gives the pipeline a
deterministic signal for "how long should this answer be" that a later
Chat/Voice dual-output sprint can consume - without yet building that
dual-output system itself.

FILES TO CHANGE:
- luno/response_policy.py (NEW - all policy logic, self-contained, pure
  function, no I/O, no LLM/network call)
- main_runtime_demo.py (PlannerBridgeModule: import the module; two new
  bounded per-conversation dicts in __init__; compute the policy once in
  _handle_utterance() alongside the other early per-turn reads
  (memory retrieval, emotion estimation); append one instruction note
  right after the persona block; pop both dicts in
  _on_conversation_ended() - five small, additive edits, no existing
  line removed or changed)
- tests/test_response_policy.py (NEW - 61 tests: 50 pure decision-matrix
  tests + 11 end-to-end tests through the real RuntimeDemoConsole
  pipeline)

DIRECTLY AFFECTED SUBSYSTEMS:
- Personality / prompt assembly (main_runtime_demo.py's PlannerBridgeModule
  system_prompt "notes" pipeline - a new note is appended after the
  persona block, no existing note is reordered/removed)

INDIRECTLY AFFECTED SUBSYSTEMS:
- LLM Manager (system_prompt content is one field of NeedLLMResponse -
  shape/contract unchanged, only content grows by one short block)
- Memory (explicitly NOT touched - luno/response_policy.py imports
  nothing from luno.memory/luno.persistence/luno.relationship_engine/
  luno.episodic_memory/luno.reminders/luno.memory_guard/habit_memory/
  luno.memory_context, verified by a dedicated source-scan test; the
  wiring in main_runtime_demo.py does not add a second
  memory_retriever.retrieve_memories() call, verified by a dedicated
  E2E call-count test)
- Dashboard (no new page/endpoint added - see "Dashboard/debug"
  decision below)

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- LLM request contract (`NeedLLMResponse{... system_prompt ...}`) - the
  new depth-instruction block is just one more string appended to
  `notes` before `"\n\n".join(notes)`; the field's shape is unchanged.
- Personality prompt contract - untouched. `build_persona_prompt()`/the
  persona block is not modified or reordered; the depth note is
  appended immediately AFTER it, verified by an integration test.
- Event contract for `conversation_ended` - unchanged. The new
  `_response_depth_context`/`_last_response_policy` pops in
  `_on_conversation_ended()` follow the exact existing pattern already
  used by `_last_device_target`/`_pending_env_confirmations`/
  `_session_feedback_target`/`_last_turn_trace` in the same method - no
  new route is registered, no existing route is changed.

EXPECTED REGRESSION RISKS:
- Low. `luno/response_policy.py` is a pure-function/rule-based module
  with no I/O, no new event subscribers, no modification of any
  existing note-building function. The single call site in
  `_handle_utterance()` is wrapped in its own try/except (same
  "compute once, own try/except, never break the turn" convention every
  other early per-turn read in that method already uses) - a policy
  failure logs and falls back to a hardcoded NORMAL-depth
  ResponsePolicy, verified by a dedicated E2E test that forces the
  computation to raise and confirms the turn still completes with
  "Response depth: NORMAL" in the prompt.
- The 5 edits to `main_runtime_demo.py` (a Protected Core file) are all
  additive (new lines only, nothing removed/rewritten) - any breakage
  would be either an import error (caught immediately by the baseline
  `test_runtime_demo.py` run) or the new note appearing incorrectly in
  the prompt (directly observable in the new integration tests).

DASHBOARD/DEBUG DECISION (Phase 4):
Inspected `luno/dashboard/collectors.py`. Every existing collector
(`collect_routing_status`, `collect_context_preview`, `collect_health`)
backs one specific, already-built dashboard page/endpoint in
`luno/dashboard/server.py` - there is no existing page a new, per-turn,
ephemeral decision like this naturally belongs on, and the brief
explicitly forbids adding a new dashboard page/UI feature this sprint.
Per the brief's own fallback ("keep the data internal and testable"),
the full last-resolved `ResponsePolicy` (depth/score/reasons/explicit/
task_type) is instead kept in a small, bounded, in-memory,
per-conversation dict (`PlannerBridgeModule._last_response_policy`),
mirroring the existing, already-established, non-dashboard-exposed
`_last_turn_trace` convention in the same class. It is inspectable via
direct attribute access (the same convention `_last_turn_trace` already
uses - no test in this codebase reads it through a public accessor
either) and via the existing `log()` line added at the same call site,
which prints `depth`/`score`/`explicit`/`reasons` for every turn.

TESTS TO RUN:
- python3 -m pytest tests/test_response_policy.py -q  (new, focused)
- python3 -m pytest tests/test_runtime_demo.py tests/test_device_context.py
  tests/test_browser_wiring.py tests/test_persona.py tests/test_emotion_engine.py -q
  (prompt-assembly / planner / conversation-boundary regression tests
  directly adjacent to what this sprint touches)
- python3 -m pytest tests/test_relationship_engine.py tests/test_episodic_memory.py
  tests/test_memory_guard.py tests/test_state_isolation.py
  tests/test_memory_regression.py tests/test_persistent_state_hardening.py -q
  (memory-system regression - this sprint must not touch any of these)
- Full tests/ sweep (51 files, per-file/small-batch due to sandbox
  timeout constraints - see Final Report's Regression section for the
  complete per-batch breakdown)

NEW TESTS REQUIRED:
- tests/test_response_policy.py - explicit SHORT/DETAILED commands,
  explicit-instruction precedence over task-type/complexity/
  conversational-context signals, simple factual/yes-no/definition
  questions, how-to/troubleshooting/comparison/tutorial/architecture
  task-type classification, multi-question and multi-concept complexity
  signals (with the "a connector word alone means nothing" guard
  explicitly tested), conversational continuation (nudge-not-reset
  behavior, never applied when the turn has its own task signal),
  "jelasin lagi"/"aku masih bingung"/"intinya aja" behavior, score
  bounds 0-100 under a stacked-signal worst case, determinism (repeated
  calls, with and without previous_score), structural proof of no
  network/LLM-call capability (source-text scan) and no memory-module
  import (source-text scan), ResponsePolicy/to_dict shape,
  build_depth_instruction() wording and non-leakage of raw score/reasons,
  plus 11 end-to-end tests through the real RuntimeDemoConsole pipeline:
  SHORT/NORMAL/DETAILED instructions actually reaching system_prompt,
  persona block still present alongside the new note, policy computed
  exactly once per turn (counting wrapper), no duplicate memory
  retrieval (counting wrapper on memory_retriever.retrieve_memories),
  isolated persistent-state files untouched by a depth-only turn,
  continuation state updating across two real turns in the same
  conversation, conversation-boundary reset (direct call, matching the
  existing `_on_conversation_ended` white-box test convention used by
  test_device_context.py/test_browser_wiring.py - see the Final Report's
  Regression section for why this file's own event is never routed to
  "planner" in this console), inspectability of the debug-only
  `_last_response_policy` dict, and defensive fallback-to-NORMAL on a
  forced policy-computation failure.

ROLLBACK PLAN:
Revert the 5 additive edits in main_runtime_demo.py and delete
luno/response_policy.py + tests/test_response_policy.py. Nothing else
in the repository references luno/response_policy.py (new module, no
other consumer), so removal is a clean, isolated revert with no
cascading changes. No persistent state or config schema was touched, so
no data migration/rollback is needed on that front either.
```
