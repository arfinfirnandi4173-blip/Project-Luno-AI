# Change Impact Analysis — Shared Experience & Episodic Memory Layer

```
FEATURE: Episodic Memory (Shared Experience layer) — a bounded, deterministic,
grounded record of MEANINGFUL shared events (technical problems solved together,
HA devices configured, project milestones, explicitly user-declared important
moments), separate from long-term FACTS (luno/memory.py's _memories) and from
whole-SESSION recaps (luno/memory.py's session_summaries).

WHY: Sprint brief requires Luno to remember "the RIGHT things, for the RIGHT
reasons, retrieved at the RIGHT time" — not a transcript hoarder. Pre-flight
audit (grep -rniE "episodic|shared.?experience|experience_id|event_id|
fingerprint" across luno/, tests/) found no existing system that does this:
  - luno/memory.py's long-term facts store explicit user-stated FACTS
    ("aku alergi kacang"), not events, and has no category/outcome/provenance
    fields.
  - luno/memory.py's session_summaries store an LLM-generated 1-3 sentence
    recap of an ENTIRE session (triggered at session end, unconditionally,
    with no meaningfulness filter, no dedup, no relevance gating — injected
    into every prompt via build_session_summary_prompt() regardless of query).
    This is the closest existing neighbor but explicitly NOT what this sprint
    wants: it has no "is this worth remembering" gate, no structured category,
    no content-based dedup, and is not retrieved through the relevance-scored
    memory_retrieval pipeline at all.
  - luno/relationship_engine.py tracks a shared_experience_count integer only
    — no record of what the experiences actually WERE.
There is no duplicate system being built here; this fills a real, distinct gap.

FILES TO CHANGE:
- luno/episodic_memory.py (NEW) — detection, validation, dedup, persistence,
  and a MemorySource factory for retrieval.
- luno/config.py — add EPISODIC_MEMORY_FILE, EPISODIC_MEMORY_MAX_ENTRIES.
- main_runtime_demo.py — register the new memory_retriever source in
  PlannerBridgeModule.__init__; call episodic_memory.observe_turn() once per
  turn in _handle_utterance, right before the existing Relationship Engine
  update block, and OR its result into that block's existing
  explicit_memory_shared signal.
- tests/test_episodic_memory.py (NEW).
- tests/test_runtime_demo.py — redirect EPISODIC_MEMORY_FILE to a temp path
  before any _new_console() call (same test-hygiene pattern already applied
  for RELATIONSHIP_STATE_FILE); add an end-to-end integration test.
- ARCHITECTURE_GUARD.md — new subsystem section + Episodic Memory Contract.

DIRECTLY AFFECTED SUBSYSTEMS:
- Memory retrieval (luno/memory_retrieval/*) — gains one new registered
  source, no changes to its own files.
- Relationship Engine — gains one additional, OR'd input signal into its
  existing explicit_memory_shared parameter. No change to RelationshipEngine/
  RelationshipState/RelationshipStore/RelationshipContextBuilder themselves.
- Prompt assembly (PlannerBridgeModule._handle_utterance) — one new
  try/except note-producing call site; no reordering of existing sections.

INDIRECTLY AFFECTED SUBSYSTEMS:
- None. luno/memory.py, luno/emotion_engine.py, luno/persona.py,
  luno/world_model.py, luno/memory_guard.py are read from (detect_remember_command,
  had_successful_tool_call) but never imported by luno/episodic_memory.py
  itself, and never mutated by it.

PROTECTED CONTRACTS (see ARCHITECTURE_GUARD.md §4):
- Memory contract (luno/memory.py) — read-only use of detect_remember_command's
  boolean result; _memories/_session_summaries schemas untouched.
- Relationship State Contract — RelationshipState/RelationshipEngine.apply()
  signatures unchanged; only the explicit_memory_shared VALUE passed in changes
  (still a plain bool, same as before).
- Memory Retrieval contract (RelevantMemory/QueryAnalysis/MemorySource shape) —
  consumed exactly as documented in sources.py's own docstring, no changes to
  retriever.py/models.py/query.py/sources.py.

EXPECTED REGRESSION RISKS:
- A new note-append call site in _handle_utterance could, if unguarded, break
  a turn — mitigated with the same try/except-and-log-skip convention every
  other note already uses.
- A new registered memory source could theoretically surface irrelevant text —
  mitigated by has_any_signal gating + narrow deterministic detection patterns
  (never registers a source that "returns everything").
- Test-file pollution of the real config/episodic_memory.json — mitigated by
  redirecting EPISODIC_MEMORY_FILE in tests/test_runtime_demo.py BEFORE any
  console construction, same fix already applied for relationship_state.json.

TESTS TO RUN:
- python -m pytest luno/ -q (full fast suite, 806 passed / 2 known-flaky baseline)
- python -m pytest tests/test_episodic_memory.py -q (new)
- python -m pytest tests/test_relationship_engine.py tests/test_emotion_engine.py
  tests/test_runtime_demo.py -q (152 passed baseline)

NEW TESTS REQUIRED:
- Creation/grounding, persistence, deduplication (same event twice, same
  summary twice, same event after simulated restart), retrieval relevance +
  bounding, temporal wording reuse, relationship integration (episodic ->
  relationship one-way, no fabricated relationship boost), isolation (no
  import of luno.memory/emotion_engine/persona/relationship_engine, verified
  via ast, not substring search), end-to-end integration test.

ROLLBACK PLAN: Revert this diff. The feature is fully additive — removing the
registered source + the two new call sites in main_runtime_demo.py restores
prior behavior exactly (the relationship engine already tolerated a plain
bool for explicit_memory_shared before this change).
```
