# Memory Retrieval End-to-End Audit (read-only)

Read-only reconnaissance sprint. No production code, tests, or config
files were modified.

## Question

When topic-history successfully identifies the correct memory/topic, does
it survive ranking, budget selection, rendering, and reach the final LLM
system prompt?

## Pipeline map

`select_topic_candidates()` (line 956) -> `topic_history_to_relevant_memories()`
(line 1039) / single-slot `active_topic_to_relevant_memory()` fallback ->
`assemble_context()` (line 1418) -> `relevant_memory_to_context_item()` ->
`_apply_decision_quality_bonus()` -> `deduplicate_context_items()` (line
1166) -> sort by `_rank_key()` (line 181) -> `_apply_budget()` (line 1258)
-> `render_context_block()` (line 1384) -> `main_runtime_demo.py`'s `notes`
list -> `system_note` -> `NeedLLMResponse.data["system_prompt"]` -> LLM
client `system_prompt` argument. All in `luno/memory_context.py` unless
noted; unchanged since the prior "Memory Retrieval & Decision Quality
(re-audit)" sprint (mtime-verified against that sprint's own edits).

`_rank_key()`: `(relevance, importance, context_evidence, usefulness,
evaluation, usage_count, intent_bonus, priority)`, lexicographic - relevance
dominates absolutely.

`_apply_budget()`: count cap (`config.max_results`) then a token-budget
loop using `continue` (not `break`) on an oversized item, so a large item
can never block a smaller, later one.

## Live probe (isolated, outside repo tree)

5-turn Indonesian scenario ("ESP32 pakai INMP441 buat voice assistant." ->
"Kalau mikrofonnya gimana?" -> "Yang tadi soal mic gimana?" -> "Kalau yang
lain?" -> "Terus untuk koneksinya?") plus an unrelated query ("Berapa
ukuran aquarium 50x25?"), driven through the real `RuntimeDemoConsole`.

| Turn | Topic Found | Candidates | Ranked | Survived Budget | Rendered | In Final Prompt |
|------|-------------|------------|--------|------------------|----------|------------------|
| 1 | N/A (first turn) | 0 | - | - | - | no block (correct) |
| 2 | Yes (ESP32/INMP441, single-slot fallback) | 1 | yes | yes | yes | **yes** |
| 3 | **No** - topic already overwritten before this turn's retrieval ran | 1 (wrong: turn 2's own thin snapshot) | yes | yes | yes | wrong content, but present |
| 4 | No (inherits turn 3's thin snapshot, preserved not replaced) | 1 | yes | yes | yes | same wrong content |
| 5 | No (same thin snapshot, preserved again) | 1 | yes | yes | yes | same wrong content |
| 6 (unrelated) | N/A | 0 | - | - | - | no block (correct) |

## Root cause

Not ranking, budget, or rendering - all three are correct pass-throughs in
every turn observed. The loss happens ONE STAGE EARLIER: at the turn-2
topic-state UPDATE (`main_runtime_demo.py::_on_assistant_response()` ->
`memory_context.update_active_topic()`). `classify_reference_type("Kalau
mikrofonnya gimana?")` returns `"comparison"`, which is excluded from
`_PURE_REFERENCE_TYPES`, so `is_pure_reference_followup()` returns `False`
-> the active topic is REPLACED with turn 2's own thin terms instead of
being preserved. Turn 3 then correctly retrieves *something* - but it's
already the wrong, overwritten snapshot.

## Regression/safety

Confirmed: the unrelated aquarium query produced zero candidates and no
memory block - ESP32/INMP441 context was not injected merely because it
was recent.

## Recommendation (conceptual only, no code in this sprint)

Extend the comparison branch's existing residual-word check with one more
condition: if the residual term(s) overlap the CURRENT active topic's own
terms (reusing the same token-overlap primitive style
`select_topic_candidates()` already uses), treat the turn as a pure
reference for state-preservation purposes; only replace when the residual
is genuinely absent from the current topic. Implemented in the immediately
following sprint - see `docs/change_impact/memory_comparison_topic_preservation.md`.

## Persistent state

Probe ran entirely outside the repo tree (`/tmp`), with every writer-
capable state file redirected to an isolated temp path before any
`RuntimeDemoConsole` was constructed (mirrors `tests/conftest.py`'s own
`_WRITABLE_STATE_ATTRS` list). `config/*.json` mtimes confirmed
byte-unchanged before and after.
