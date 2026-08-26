# Change Impact Analysis — Relationship Engine Foundation

Filled out from `docs/templates/CHANGE_IMPACT_ANALYSIS.md`.

```
FEATURE:
Relationship Engine Foundation

WHY:
Give Luno a persistent, deterministic, testable representation of
relationship state (familiarity/trust/closeness/interaction history)
that can influence future behavior subtly, without becoming a second
Memory system, without letting the LLM write scores directly, and
without disturbing any existing subsystem's contracts.

FILES TO CHANGE:
- luno/relationship_engine.py (NEW - state model, deterministic update
  policy, JSON persistence, compact prompt-context builder; all in one
  self-contained module, same "one file, several small classes"
  convention luno/memory_guard.py and luno/emotion_engine.py already use)
- luno/config.py (1 new additive env-driven constant: RELATIONSHIP_STATE_FILE)
- main_runtime_demo.py (PlannerBridgeModule: instantiate engine +
  load persisted state in __init__, classify + apply + persist once per
  turn in _handle_utterance, inject compact context note - three small,
  additive edits, no existing line removed or changed)

DIRECTLY AFFECTED SUBSYSTEMS:
- Personality / prompt assembly (main_runtime_demo.py's PlannerBridgeModule
  system_prompt "notes" pipeline - one more optional note appended, no
  existing note reordered/removed)

INDIRECTLY AFFECTED SUBSYSTEMS:
- LLM Manager (system_prompt content is one field of NeedLLMResponse -
  shape/contract unchanged, only content occasionally grows by one short
  block)
- Memory (READ-ONLY signal: reuses the existing public, stateless
  `memory.detect_remember_command(text)` to detect "user explicitly
  asked to be remembered" as a grounded relationship signal - never
  calls `memory.add_memory()`/`remove_memory()`/anything mutating, never
  duplicates memory content into relationship state)
- Emotion Engine (READ-ONLY, optional: the current `UserEmotionState` is
  threaded into the relationship classifier's call signature for
  architectural correctness against the sprint's own dependency diagram,
  but the MVP classifier does not derive any state delta from it - see
  "EXPECTED REGRESSION RISKS" below and the module's own docstring)

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- LLM request contract (`NeedLLMResponse{... system_prompt ...}`) - the
  new relationship-context block is one more optional string appended to
  `system_note`; the field's shape is unchanged.
- Personality prompt contract - untouched. `build_persona_prompt()` is
  not called, modified, or reordered. The new note is appended in the
  same region as the Emotion Engine's own note (after persona/memory/
  verified-facts/session-summary notes, before the final language/
  character-reminder note) - verified by a new integration test.
- Memory contract - `memory.detect_remember_command()` is an existing,
  already-public, stateless, side-effect-free function - calling it a
  second time from a new call site changes nothing about its contract.

EXPECTED REGRESSION RISKS:
- Low. Pure-Python, rule-based, in-memory-plus-one-small-JSON-file
  module with no I/O beyond that file, no new event subscribers, no
  modification of any existing note-building function. The call site in
  `_handle_utterance` is wrapped in the same try/except-and-log pattern
  every other note already uses - a failure here degrades to "no
  relationship-context note this turn," never a broken turn.
- Threading `emotion_state` through the classifier without using it yet
  is intentionally inert - documented explicitly so it cannot be
  mistaken for a forgotten TODO; no behavior depends on it in this
  sprint.

TESTS TO RUN:
- python3 -m pytest luno/ -q (FAST suite)
- python3 -m pytest tests/test_relationship_engine.py -q (new, focused)
- python3 -m pytest tests/test_emotion_engine.py tests/test_persona.py tests/test_memory_guard.py tests/test_memory_retrieval.py tests/test_memory_regression.py tests/test_runtime_demo.py -q
  (regression tests directly adjacent to the prompt-assembly, memory-
  separation, and emotion-independence guarantees this feature must not
  violate)

NEW TESTS REQUIRED:
- tests/test_relationship_engine.py - state init (missing/empty/default),
  validation (valid/invalid numeric/missing fields/wrong types/unknown
  fields), persistence (save/load/round-trip/malformed JSON/wrong root
  type/partial state/unknown schema version), bounds (-999/-1/0/0.5/1/2/
  999/NaN/Infinity), update model (technical-neutral/successful-task/
  correction/explicit-memory-shared-experience/neutral), determinism
  (same event sequence -> same state), isolation (never touches Memory/
  Emotion Engine/Persona/LLM config), prompt-context builder (empty for
  a brand-new relationship, compact/banded for an established one, never
  contains the VERIFIED-facts marker).
- tests/test_runtime_demo.py - one new end-to-end test proving a real
  turn updates + persists relationship state and a later turn's
  system_prompt can carry the relationship-context note alongside
  persona/verified-facts/language-override, in the correct relative
  order, without breaking the existing persona/verified-facts/emotion
  integration tests' own assertions; plus one test proving a purely
  technical/device-command turn produces no relationship-context note.

ROLLBACK PLAN:
Revert the 3 additive edits in main_runtime_demo.py and delete
luno/relationship_engine.py + the 1 new config constant + the new test
files + config/relationship_state.json (if created). Nothing else in
the repository references luno/relationship_engine.py (new module, no
other consumer), so removal is a clean, isolated revert with no
cascading changes.
```
