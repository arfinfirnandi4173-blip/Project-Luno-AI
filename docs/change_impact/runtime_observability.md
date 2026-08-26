# Sprint 50 — Runtime Observability, Test Logging & Real-World Data Capture

## Goal

Build a lightweight, production-safe observability and test-data logging
system for Luno — OBSERVABILITY ONLY, no intelligence-behavior change.
After this sprint: important runtime events are visible from a
dashboard/console, saveable to human-readable text logs and
machine-readable JSONL logs, real conversations and their memory/
reference decisions can be captured, exported as structured test cases,
replayed against the current code, and compared expected-vs-actual — and
the resulting logs/docs are sufficient for a fresh Claude Pro account to
continue the project without this chat history.

## Phase 0 reconnaissance findings

Before writing any code, the existing infrastructure was read end to
end. It is far more mature than a from-scratch build would assume:

- A real Event Bus (`luno/core/event_bus.py`, `luno/core/events.py`) —
  every module already publishes/subscribes through it.
- A full HTTP dashboard (`luno/dashboard/` — `server.py`, `collectors.py`,
  `controls.py`, `events_buffer.py`, `logs_buffer.py`, `voice_latency.py`)
  serving a "Brain Debugger" panel (ARCHITECTURE_GUARD.md §32) that
  already shows memory turn traces, decision traces, retrieval funnels,
  topic-history timelines, quality metrics, and voice-pipeline latency —
  all read-only, all built from state a production call already computed.
- `EventRingBuffer`/`StatsAggregator`/`VoiceLatencyRecorder` — all
  passive Event Bus observers (`subscribe("*", ..., priority=-1000)`),
  bounded, in-memory only.
- `LogCapture` — a `sys.stdout` tee capturing every `log()`/
  `log_lifecycle()` print line into a bounded ring buffer.
- `MemoryTurnTrace`/`build_turn_trace()` (`luno/memory_turn_trace.py`) —
  a per-turn, non-persistent record of memory candidate/selection state,
  extended in Sprint 32 with `reference_type`/`active_topic_terms`/
  `funnel`, never raw conversation text.
- `ProductionConsole` (`luno/bootstrap/console.py`) — the real developer
  command interface (`/status /health /events /debug ...`).

What was genuinely missing (confirmed via targeted `grep`, not assumed):

1. **Nothing persisted the Event Bus's own event stream to disk.**
   `EventRingBuffer`/`LogCapture` are both deliberately in-memory-only
   (bounded `deque`s) — built for the dashboard's own "last N" pages, not
   durable, across-restart storage.
2. **The memory/reference/topic decision pipeline never published a
   single Event Bus event of its own.** `PlannerBridgeModule.
   _handle_utterance()` computes `reference_type`/`is_short_followup`/
   which of 4 topic-resolution branches fired/whether Sprint 48's or
   Sprint 49's own ambiguity gates refused — and only ever `log()`-prints
   it, or leaves it inside `MemoryTurnTrace` for the dashboard's own
   pull-based inspectors. Nothing could ever *push* "this turn was
   refused for ambiguity reasons" as a discrete, timestamped, externally
   observable fact.
3. **No real-world test-data capture/replay/diff mechanism existed at
   all** (`grep -rliE "mark_test|real_world|replay_engine"` returned
   nothing in the whole tree before this sprint).

Per the brief's own explicit instruction ("DO NOT create a second
competing logging framework if an existing mechanism can be extended
safely"), this sprint extends items 1-2 above rather than replacing them,
and builds item 3 fresh since nothing existed to extend.

## Phase 1 — the observability event model

Rather than the brief's own suggested 15-category list, a **smaller,
closed model** was implemented — each type backed by a real call site
reading data `_handle_utterance()` already computes:

| Event type | Published from | Carries |
|---|---|---|
| `memory_reference_classified` | right after `reference_type`/`is_short_followup` are computed | reference_type, is_short_followup, query_intent |
| `memory_topic_decision` | right after the 4-branch topic-resolution chain | topic_decision label, active_topic_terms, topic_age, ambiguity_check_result, ambiguity_refusal |
| `memory_selection_summary` | right after `MemoryTurnTrace` is built | funnel, candidate/selected counts |
| `test_case_captured` | `mark_test_case()` | case_id, status, scenario, turn count |
| `test_case_replayed` | `replay_case()` | case_id, result, primary_difference |
| (existing) `user_utterance`/`assistant_response` | unchanged | already cover USER_INPUT/ASSISTANT_RESPONSE — not duplicated |

`ENTITY_RESOLUTION`/`TEMPORAL_CLASSIFICATION`/`CONTEXT_ASSEMBLY`/
`RESPONSE_SELECTION`/`AMBIGUITY_REFUSAL` from the brief's own suggested
list are folded into `memory_topic_decision`/`memory_selection_summary`
above rather than given their own event type, because this
architecture's own decision-making does not have separate stages for
them — one topic-decision branch (ordinal / topic-history / active-topic
/ temporal-fallback / refuse) already covers entity resolution, temporal
classification, and ambiguity refusal as ONE decision, not four.

`topic_decision` is one of: `ORDINAL_RESOLVED`, `MERGE_TOPIC_HISTORY`,
`MERGE_ACTIVE_TOPIC`, `MERGE_TEMPORAL_FALLBACK`, `NO_CANDIDATE`. It is
computed via pure additive assignment statements inside branches that
already existed (zero control-flow change) — verified via the existing
targeted regression suite (184 tests) passing unchanged immediately
after instrumentation, before any new test was even written.

`ambiguity_check_result` is captured via a **walrus-operator wrapper
around the existing `is_active_topic_relevant_to_query()` call**
(`main_runtime_demo.py`, inside the `elif` at the "single-slot active
topic" branch) — same function, same two arguments, same short-circuit
laziness (Python's own `or` still skips the call when unnecessary, so
the field stays honestly `None` for turns where the guard never ran).
This is the mechanism that makes Sprint 48's and Sprint 49's own
ambiguity gates — previously provable only via unit tests — now visible
as a live, timestamped event.

Live E2E verification (before any test file was written): running the
Aquascape A/B sequence from Sprint 49 through a real `RuntimeDemoConsole`
produced, on the third turn, `memory_topic_decision` with
`topic_decision=NO_CANDIDATE`, `ambiguity_check_result=False`,
`ambiguity_refusal=True` — the exact Sprint 49 gate, now externally
observable for the first time.

**Privacy:** none of the 5 new event types ever include the raw
utterance text — only bounded labels/counts/term-sets, matching
`MemoryTurnTrace`'s own long-standing "never raw conversation text"
privacy boundary. Every publish is wrapped in its own `try/except: pass`
— a telemetry failure can never break a turn (verified directly:
`test_13_a_telemetry_publish_failure_cannot_break_a_turn`).

## Phase 2-4 — the logging layer

New file: `luno/dashboard/event_log_writer.py`, class `EventLogWriter`.
Built with the EXACT SAME technique `EventRingBuffer`/`StatsAggregator`/
`VoiceLatencyRecorder` already use (`event_bus.subscribe("*", self.
_on_event, priority=-1000)`, every handler `try/except: pass`,
unsubscribe in `stop()`) — pointed at two NEW destinations neither
pre-existing component writes to:

- `logs/events/YYYY-MM-DD.jsonl` — one JSON line per event, date-rotated.
- `logs/runtime/YYYY-MM-DD.log` — human-readable text, date-rotated. The
  6 Sprint 50 event types get the brief's own Phase 3 multi-line
  rendering (`SESSION:`/`TURN:`/`EVENT:`/per-field lines); every other
  pre-existing event type gets one compact line matching this project's
  own `[HH:MM:SS.mmm] [Luno.<component>] <message>` convention — so the
  file reads in the SAME visual language the rest of the project already
  uses, not a second logging dialect.

**Privacy/bounding:** `_redact()` recursively replaces any dict value
whose KEY matches `api[_-]?key|password|token|secret|authorization|
credential|bearer` (case-insensitive) with `***REDACTED***`, applied to
BOTH file formats before they are ever written. `_bound_value()`
separately caps any string field at 500 characters
(`...[truncated]`) as defense-in-depth (this project's own event
publishers never put raw prompt text in `data` in the first place).

**Failure isolation:** every file operation (`os.makedirs`, `open`,
`write`) has its own `try/except: pass`; a write failure increments a
`write_failures` counter but never raises, never blocks the bus, and
never stops other subscribers from receiving the same event — verified
directly (`test_25_write_failure_isolated_never_raises`, which points
the writer at a path that cannot be a directory and confirms a sibling
subscriber still receives the event).

**Rotation:** on construction, deletes any `YYYY-MM-DD.{jsonl,log}` file
older than `max_retention_days` (default 14) in either directory —
best-effort, never raises.

**Off by default:** `RuntimeDemoConsole.__init__` gained
`enable_observability_log: bool = False` and `observability_log_dir:
str = "logs"` — defaulting OFF so the ~2900 pre-existing tests that
construct a console every day get byte-for-byte identical behavior and
zero new files anywhere (verified: `test_27_disabled_by_default_no_new_
files_for_ordinary_console`). `DashboardServer.start()` DOES wire it
unconditionally (same lifecycle as `EventRingBuffer` etc.) since the
dashboard is itself already an opt-in observability surface — a
`observability_log_dir` constructor parameter lets callers (including
this sprint's own tests, and `tests/test_memory_voice_observability.py`'s
pre-existing E2E test, updated this sprint) point it at a temp directory
instead of the real repository tree.

## Phase 5-6 — dashboard panel & bounded session trace

Because `EventRingBuffer` already subscribes to `"*"`, the pre-existing
generic Event Bus live page **automatically shows all 5 new event types
with zero dashboard code change** — confirmed via a real HTTP round-trip
in `test_32_e2e_observability_routes_through_real_dashboard_http`. This
satisfies the brief's own item 1 ("see important runtime events from a
dashboard/console") without a second display mechanism.

Two small, additive, read-only collectors were added anyway, matching
the "at minimum" field list Phase 5 asks for, mirroring the Sprint-32
Brain Debugger's own "pure `Runtime state -> JSON-safe dict`" convention
exactly (no new computation, no new store):

- `collect_observability_summary()` — reference classification, topic
  decision (including the new `ambiguity_refusal`), selection counts,
  funnel — plus a derived `status` (`PASS`/`REFUSED`/`NO_CANDIDATE`).
  Routed at `GET /api/observability/summary`.
- `collect_session_trace()` — Phase 6's own 8-stage pipeline diagram
  (USER_INPUT → CLASSIFICATION → REFERENCE_RESOLUTION → TOPIC_UPDATE →
  MEMORY_CANDIDATES → MEMORY_SELECTION → CONTEXT_ASSEMBLY →
  ASSISTANT_RESPONSE), rendered from `MemoryTurnTrace` fields, already
  bounded by the pre-existing `_turn_trace_history` `deque(maxlen=100)`
  (Sprint 32) — no new storage. Routed at
  `GET /api/observability/session_trace`.

**Honest limitation, not fabricated precision:** neither collector
surfaces the raw "latest user input"/"latest assistant response" TEXT
the brief's own Phase 5 example shows — `MemoryTurnTrace` has never
stored raw conversation text, and this sprint does not create an
exception to that boundary. That text is already visible elsewhere in
the SAME dashboard, unredacted, via the pre-existing Event Bus/Logs
pages (`speech_recognized`/`assistant_response` events already carry a
`text` field).

No change was made to `luno/dashboard/static/index.html` — the two new
routes are consumable by any client (including a future dashboard panel)
without requiring one; this is a deliberate, documented scope decision,
not an oversight, given the existing generic Event Bus page already
covers the sprint's own item 1.

## Phase 7-8 — real-world test data capture & export

New file: `luno/test_capture.py`. `mark_test_case(console, conversation_id=None, note="", scenario="", base_dir=...)`:

- Reads TWO already-existing, already-bounded pieces of state (creates
  no new raw-text store): `console.conversation_log` (`Deque[Tuple[str,
  str]]`, already maintained by both `RuntimeDemoConsole` and
  `ProductionConsole` for their own `/history` command since well before
  this sprint) and `console.planner_module._last_turn_trace` (the
  bounded, non-text `MemoryTurnTrace`).
- `conversation_id=None` falls back to the most recently recorded turn
  overall — the same fallback convention `collectors.py::_find_trace()`
  already established, reused here for consistency.
- Writes directly to the FINAL case JSON shape (id, source, scenario,
  status, captured_at, conversation_id, conversation, actual, expected,
  note, metadata) at `tests/real_world/candidate/real_NNNNNN.json` — no
  separate "export" step; capture already produces the target format.
  Conversation lines are user-channel only, bounded to the last 12 turns,
  each capped at 500 characters.
- `/mark_test [note]` was added to `ProductionConsole`'s own command
  dispatch (a thin relay, matching that class's own established
  discipline) plus `console.mark_test(...)` directly on
  `RuntimeDemoConsole` for scripted use.

## Phase 9-10 — replay engine & expected/actual diff

New file: `luno/replay.py`. `replay_case(case)` spins a FRESH
`RuntimeDemoConsole` (the same production `PlannerBridgeModule`/
`memory_context` pipeline every prior sprint's own E2E tests exercise —
never a second/simplified classifier), feeds `case["conversation"]`
through it with a deliberately generic, never-leaking canned reply
("Baik, dicatat." — same anti-leak discipline this project's own probe
scripts have used since Sprint 46), then compares the resulting
`MemoryTurnTrace`'s own fields against `case["expected"]`.

Never calls a real LLM — fully deterministic, verified directly
(`test_06_replay_never_calls_a_real_llm`: same case replayed twice
produces byte-identical `actual` output both times).

Three verdicts: `PASS` (every expected field matched — `required_terms`/
`active_topic_terms` checked as a subset, everything else exact),
`FAIL` (with `primary_difference`/`secondary_difference`), `REVIEW`
(case has no `expected` block yet — an unannotated candidate is not
judged, matching Phase 8's own "do not require a human to manually
rewrite the entire conversation").

`format_diff()` renders the brief's own Phase 10 worked example shape
(`CASE:` / `EXPECTED:` / `ACTUAL:` / `RESULT:` / `PRIMARY DIFFERENCE:` /
`SECONDARY DIFFERENCE:`), generated from whatever fields the case
actually carries.

## Phase 11-12 — data quality gating & storage layout

`_VALID_STATUSES = ("candidate", "reviewed", "approved", "rejected")` is
a strict enum enforced by `set_case_status()` (an invalid status is
rejected, returns `False`, changes nothing). A case is born `"candidate"`
and NOTHING in `mark_test_case()` ever promotes it further — only an
explicit `set_case_status(..., "approved")` call does. `replay_all()`
defaults to `status="approved"` — a `candidate`/`reviewed`/`rejected`
case is never silently swept into a regression run (verified directly:
`test_21_replay_all_only_reads_approved_by_default`).

Storage layout (adapted from the brief's own suggested tree — directory
names use the SAME singular spelling as the status enum itself, to avoid
a naming mismatch between the two):

```
logs/
    runtime/YYYY-MM-DD.log
    events/YYYY-MM-DD.jsonl
tests/real_world/
    candidate/
    reviewed/
    approved/
    rejected/
```

`EventLogWriter`'s own `max_retention_days` (default 14) bounds
`logs/`'s own growth via best-effort deletion of old dated files at
construction time.

## Fix summary (production code changed)

- `luno/memory_turn_trace.py` — two new additive `MemoryTurnTrace` fields
  (`topic_decision`, `ambiguity_check_result`), one derived property
  (`is_ambiguity_refusal`), two new additive `build_turn_trace()` kwargs
  — every pre-existing call site's own defaults are unaffected.
- `main_runtime_demo.py` — 5 new `self._event_bus.publish(...)` call
  sites (own try/except each), a handful of pure additive assignment
  statements setting `_topic_decision`/`_topic_relevance_check_result`
  inside branches that already existed, one walrus-operator wrapper
  around a pre-existing function call, `RuntimeDemoConsole.__init__`
  gained two opt-in constructor parameters + lazy `EventLogWriter`
  construction in `start()`/`stop()`, plus `console.mark_test()`.
- `luno/dashboard/server.py` — `EventLogWriter` wired into `start()`/
  `stop()` (same lifecycle as `EventRingBuffer` etc.), one new
  constructor parameter (`observability_log_dir`), two new GET routes.
- `luno/dashboard/collectors.py` — two new pure collector functions.
- `luno/bootstrap/console.py` — `/mark_test` command + `mark_test()`
  thin-relay method.
- `tests/test_memory_voice_observability.py` — its own pre-existing E2E
  dashboard test updated to pass a temp `observability_log_dir`, so it
  doesn't gain a new side effect of writing real files into the
  repository's `logs/` directory every time it runs (discovered during
  this sprint's own regression sweep, fixed immediately).

No changes to `luno/memory.py`, `luno/memory_retrieval/`, ranking
(`_rank_key()`), `_apply_budget()`, `ActiveTopicSnapshot`, topic history,
TTS, streaming architecture, or any existing ambiguity/classification
rule's own OUTPUT — every new field is populated from a value that was
ALREADY being computed, never a new decision.

## E2E results

- `memory_topic_decision` correctly reports `ambiguity_refusal=True` on
  the Aquascape A/B third turn (the Sprint 49 gate), and
  `MERGE_ACTIVE_TOPIC`/`ambiguity_refusal=False` on a genuine
  ESP32/INMP441 wireless follow-up (the positive control).
- `mark_test_case()` → `set_case_status(..., "approved")` →
  `replay_all()` exercised as one continuous loop through a REAL
  `RuntimeDemoConsole`
  (`test_23_e2e_capture_then_approve_then_replay_full_loop`) — the
  brief's own "REAL CONVERSATION → LOGGED → MARKED AS TEST CASE →
  REPLAYED" loop, working end to end.
- `test_32_e2e_observability_routes_through_real_dashboard_http` proves
  both new routes AND the pre-existing generic Event Bus page all see
  the new event types through a REAL, running `DashboardServer` HTTP
  server (`requests`, not an in-process function call).

## Tests

3 new files, 47 tests total, all passing:

- `tests/test_runtime_observability.py` (22 tests) — `MemoryTurnTrace`
  field additions, event model E2E, `EventLogWriter` (JSONL/text/
  redaction/bounding/failure-isolation/rotation/opt-in), dashboard
  collectors + real HTTP E2E, performance, cross-conversation isolation.
- `tests/test_real_world_capture.py` (13 tests) — `mark_test_case()` E2E,
  privacy/bounding on captured text, id allocation, status lifecycle,
  `ProductionConsole`'s own thin-relay contract.
- `tests/test_replay_engine.py` (12 tests) — PASS/FAIL/REVIEW verdicts,
  diff formatting, approved-only gating, the full capture→approve→replay
  loop.

## Regression

Targeted (Sprint 50's own 3 new files + the full Sprint 43-49 core suite
+ `test_memory_voice_observability.py`): 775 passed, 0 failed.

Full repository sweep, run in 8 chunks (100 test files this sprint, up
from 97 — the 3 new files): **2947 collected, 2937 passed, 10 failed, 2
uncollectible files** — every single failure/uncollectible identical to
the standing baseline documented in `docs/testing/regression_baseline.md`
(6× `test_mic_device_index.py`, 1× `test_production_launcher.py::test_07`,
2× `test_real_adapters.py`, 1× `test_state_isolation.py`'s own
`inspect.getsource` sandbox gap; `test_main_bargein.py`/
`test_root_main_bargein.py` uncollectible, both dependency-related).
**Zero new regressions.** No flake required re-classification this
sprint (the Sprint 49-documented `test_streaming_e2e.py` flake did not
reproduce this run — flakes do not always reproduce, this is expected
and not itself evidence of anything).

## Performance

- `EventLogWriter._on_event()` (JSONL + text write combined, real disk
  I/O, not mocked): mean 0.122ms, min 0.090ms, max 0.513ms over 500
  calls — far under the 5ms/call target.
- `_redact()` alone: mean 0.004ms, min 0.003ms, max 0.073ms over 5000
  calls — far under the 5ms/call target.

Both also recorded in `ARCHITECTURE_GUARD.md` §50 and
`project_handover.json`'s own `tests.performance` block.

## Persistent-state safety

`config/*.json` (15 files) SHA256-hashed before/after: (1) the full
8-chunk sweep — byte-identical; (2) an isolated run of Sprint 50's own 3
new test files alone — byte-identical; (3) an isolated run of those 3
files plus the full Sprint 43-49 core suite — byte-identical. No new
`config/*.json` key, no new persistence path.

`logs/` and `tests/real_world/{candidate,reviewed,approved,rejected}/`
are new, but neither is `config/` — they are this sprint's own bounded,
documented, git-trackable output directories, not a change to Luno's
persistent memory/config schema.

One real, minor side effect was found and fixed during this sprint's own
regression sweep (not left as a known limitation): `tests/
test_memory_voice_observability.py`'s pre-existing E2E dashboard test
did not pass `observability_log_dir`, so running it created a real
`logs/` directory in the repository — fixed by pointing it at a temp
directory (see Fix summary above). Two log files created by that gap
BEFORE the fix landed remain in the repository's own `logs/` directory
(this sandbox environment does not permit deleting files under the
mounted workspace root from a shell command — see
`docs/project_handover.md`'s own takeover notes) — they are Sprint 50's
own harmless JSONL/text output (verified redaction-clean, no secrets),
not corruption of any `config/*.json` state, and the underlying test
that created them no longer does so.

## Known limitations

1. **No dashboard HTML panel was added** for the two new
   `/api/observability/*` routes — a deliberate scope decision, not an
   oversight, since the pre-existing generic Event Bus page already
   shows the new event types with zero code change. A future sprint
   could add a dedicated panel purely for presentation polish.
2. **`collect_observability_summary()`/`collect_session_trace()` never
   show raw user/assistant text** — by design, preserving
   `MemoryTurnTrace`'s own long-standing privacy boundary. The text is
   available elsewhere in the same dashboard (Event Bus/Logs pages).
3. **Replay's canned reply is a fixed generic string**, not a
   reproduction of what the real LLM actually said during the original
   captured conversation — intentional (this project's own established
   anti-leak discipline: a domain-specific canned reply could
   accidentally supply the "correct" answer the classifier is supposed
   to derive from the USER's own words alone). A future sprint COULD
   store the original assistant replies too (bounded, privacy-reviewed)
   if a real need for higher-fidelity replay is proven via live
   reproduction.
4. **Two stray log files from a since-fixed test gap remain in `logs/`**
   in this checkout (see Persistent-state safety above) — cannot be
   deleted from this sandbox; harmless.

## Invariants (restated, unchanged by this sprint)

No embeddings, no LLM judge, no second ranking/memory system, no
unbounded entity graph, no persisting raw conversation solely for
provenance, no weakened ambiguity refusal, no world-knowledge
fabrication. This sprint adds NO new intelligence decision — every
`topic_decision`/`ambiguity_check_result` value published is a value the
architecture was already computing; this sprint only makes it
observable.

## Recommended Sprint 51

Sprint 49's own recommended next step (query-side entity differentiator
matching — see `docs/project_handover.md` §22) remains the most
promising INTELLIGENCE-track item, deliberately not touched this sprint
(this sprint is observability-only, per its own explicit non-negotiable).
Separately, on the observability track itself: once a real user's own
conversations have produced a handful of genuinely reviewed/approved
real-world cases via `/mark_test`, a future sprint could audit whether
`replay.py`'s own generic-canned-reply limitation (see Known Limitation
3 above) is actually costing real fidelity, before deciding whether to
invest in richer replay.
