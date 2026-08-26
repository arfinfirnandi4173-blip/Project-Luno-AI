# Change Impact Analysis — Emotion Engine

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md` per
`ARCHITECTURE_GUARD.md` §9 (this change touches a Protected Core
subsystem - the Personality/prompt-assembly path in `main_runtime_demo.py`).

```
FEATURE:
Emotion Engine - user-emotion-aware conversational adaptation.

WHY:
Make Luno emotionally context-aware and behaviorally adaptive, without
touching verified-fact grounding, personality identity, or any other
stable subsystem. Additive only.

FILES TO CHANGE:
- luno/emotion_engine.py (NEW - all engine logic, self-contained)
- luno/config.py (2 new env-driven constants, additive)
- main_runtime_demo.py (PlannerBridgeModule: instantiate tracker in
  __init__, observe+inject in _handle_utterance, reset in
  _on_conversation_ended - three small, additive edits, no existing
  line removed or changed)

DIRECTLY AFFECTED SUBSYSTEMS:
- Personality / prompt assembly (main_runtime_demo.py's PlannerBridgeModule
  system_prompt "notes" pipeline - a new note is appended, no existing
  note is reordered/removed)

INDIRECTLY AFFECTED SUBSYSTEMS:
- LLM Manager (system_prompt content is one field of NeedLLMResponse -
  shape/contract unchanged, only content grows by one optional block)
- Memory (explicitly NOT touched - Emotion Engine never calls
  memory.add_memory()/remove_memory(); this is verified by a dedicated
  test, see below)

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- LLM request contract (`NeedLLMResponse{... system_prompt ...}`) - the
  new emotional-context block is just one more optional string appended
  to `system_note` before `"\n\n".join(notes)`; the field's shape is
  unchanged.
- Personality prompt contract - untouched. `build_persona_prompt()` is
  not called, modified, or reordered by this change. The new note is
  appended AFTER persona/memory/verified-facts/session-summary notes
  and BEFORE the final language/character-reminder note (mirrors the
  spec's own suggested ordering; verified by a new integration test
  asserting `"VERIFIED results" ... < emotional-context text < "FINAL
  INSTRUCTION"`).

EXPECTED REGRESSION RISKS:
- Low. The engine is a pure-function/rule-based module with no I/O, no
  new event subscribers, no modification of any existing note-building
  function. Worst case if something goes wrong internally: caught by a
  try/except at the single call site (same pattern every other note in
  `_handle_utterance` already uses for persona/memory/vision/etc.),
  logged, and the turn proceeds with system_prompt unchanged from
  today's behavior (no emotional-context note appended).
- The only "risk" that is not purely additive is the 3 small edits to
  `main_runtime_demo.py` (a Protected Core file) - each edit only ADDS
  lines, none removes/rewrites existing ones, so any breakage would be
  either an import error (caught immediately by baseline test) or the
  new note appearing (or not) in the prompt - both directly observable
  in the new integration test.

TESTS TO RUN:
- python3 -m pytest luno/ -q  (FAST suite - see ARCHITECTURE_GUARD.md §5)
- python3 -m pytest tests/test_emotion_engine.py -q  (new, focused)
- python3 -m pytest tests/test_persona.py tests/test_memory_guard.py tests/test_memory_retrieval.py tests/test_memory_regression.py tests/test_runtime_demo.py -q
  (subsystem/regression tests directly adjacent to the prompt-assembly
  and memory-separation guarantees this feature must not violate)

NEW TESTS REQUIRED:
- tests/test_emotion_engine.py - estimator (all listed emotion
  categories, ambiguous/mixed input, low-confidence input, unknown/no-
  signal input), decay/state-tracking (replacement by new evidence,
  time decay, current-context-takes-precedence), response policy
  (per-emotion deltas, low-confidence gate, technical_depth invariant
  never changed), prompt-block builder (empty on unknown/low-confidence,
  contains uncertainty-hedging language when present, never mentions
  verified facts), memory-separation (engine never calls
  memory.add_memory), error-fallback (malformed/None input never
  raises).
- tests/test_runtime_demo.py - one new end-to-end test proving a real
  turn's system_prompt can carry the emotional-context note alongside
  persona/verified-facts/language-override, in the correct relative
  order, without breaking the existing persona/verified-facts
  integration test's own assertions.

ROLLBACK PLAN:
Revert the 3 additive edits in main_runtime_demo.py and delete
luno/emotion_engine.py + the 2 new config constants + the new test
files. Nothing else in the repository references luno/emotion_engine.py
(new module, no other consumer), so removal is a clean, isolated revert
with no cascading changes.
```
