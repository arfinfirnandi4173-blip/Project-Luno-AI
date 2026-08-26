# Luno Brain Debugger - Memory & Voice Observability Dashboard

**Status:** Complete. **Scope:** OBSERVABILITY ONLY - this sprint adds a
read-only debugging surface over decisions Luno's existing memory,
retrieval, ranking, response, and voice pipelines already make. Nothing
in this document's changes alters what Luno remembers, retrieves, ranks,
says, or how it synthesizes/plays speech.

## 1. Existing Dashboard Architecture (Phase 0 findings)

The Dashboard (`luno/dashboard/`) is a stdlib-only (`http.server` +
Server-Sent Events, zero new dependencies) HTTP surface built on top of
the SAME already-running `Runtime`/`AdapterManager`/`modules` dict
`main.py`/`main_runtime_demo.py` construct - `luno/bootstrap/dashboard.py::
register_dashboard()` wires a `DashboardServer` around them,
`DASHBOARD_ENABLED=false` disables it entirely. `DashboardServer`
(`server.py`) is pure HTTP/JSON plumbing: `_dispatch_get()`/
`_dispatch_post()` route to `collectors.py` (read-only "Runtime state ->
JSON dict" functions, one per view) and `controls.py` (the ONLY file
allowed to mutate - always through an existing public method or a real
published Event, never a shortcut). `events_buffer.py`'s
`EventRingBuffer`/`StatsAggregator` are the established pattern for
"anything the dashboard derives purely by watching the Event Bus"
(`subscribe("*", handler, priority=-1000)`, `try/except: pass` around
every handler body). `logs_buffer.py::LogCapture` tees stdout into a
bounded ring buffer and ALREADY supports `request_id`-filtered lookup
(`snapshot(request_id=X)`) - this sprint's voice per-chunk detail reuses
this mechanism rather than adding new instrumentation to `fish_audio.py`.
`luno/memory_turn_trace.py::MemoryTurnTrace`/`build_turn_trace()` (a
PRIOR sprint's own "Memory Outcome Telemetry" work) was already the
correct, pre-existing extension point for Phase 1 - it never stores raw
user/assistant text (only ids/scores/short reason strings), which became
this sprint's own privacy boundary for the new collectors too, since a
raw query literally cannot be shown (never captured in the first place).
`static/index.html`'s nav/panel/subtab pattern (`data-panel`, per-panel
`loaders` dict in `onPanelShown()`, a 3-second poll of only the active
panel) was reused unmodified for the new "Brain Debugger" panel.

## 2. New Telemetry Architecture

Three additive layers, each answering "where was this already computed?"
before adding anything:

1. **`assemble_context(funnel=...)`** - a write-only, optional dict
   parameter filled at the exact points `luno/memory_context.py`'s
   pipeline already computes candidate/dedup/ranking/budget counts.
2. **`MemoryTurnTrace` extension + `PlannerBridgeModule._turn_trace_history`** -
   additive fields on the pre-existing per-turn trace object, plus a new,
   bounded, cross-conversation ring buffer populated in the SAME
   `try/except` block that already wrote the pre-existing
   `_last_turn_trace` dict each turn.
3. **`VoiceLatencyRecorder` + `parse_chunk_timeline_from_logs()`**
   (`luno/dashboard/voice_latency.py`, new file) - a passive Event Bus
   observer for coarse per-request timing (built entirely from events
   every real adapter already publishes) plus a pure log-parsing function
   for fine per-chunk detail (reusing `LogCapture`, never touching
   `fish_audio.py`).

All three are strictly additive: every pre-existing caller/test that
doesn't pass the new optional parameters behaves byte-for-byte as before.

## 3. Memory Decision Trace (Phase 1)

`collect_memory_decision_trace(modules, turn_id="", conversation_id="",
check_memory_id="")` (`collectors.py`) answers "why did Luno remember
this turn's context?" for one turn, reading `MemoryTurnTrace` fields a
real production call already populated: `query` (intent, reference type,
short-followup flag, category - never raw text), `topic_state` (active
topic terms, topic-history snapshot with per-entry `referenced`/
`produced_candidate` flags), `retrieval` (called?, candidate count),
`candidates` (per-memory `RENDERED`/`NOT SELECTED` status + relevance +
reason), `other_selected` (verified-fact/episodic ids), and the `funnel`
block (Phase 2). `check_memory_id` answers the SECOND question ("why did
Luno NOT remember that?") for a specific id: `RENDERED`, `NOT SELECTED`,
`NOT DETECTED` (never became a candidate this turn), or `NO CANDIDATE`
(no candidates at all this turn). `collect_memory_turn_list()` backs the
turn picker (Phase 5's inspector), listing recent turns'
id/conversation/timestamp/intent/reference-type/rendered-count only.

**Known, disclosed limitation:** per-candidate status can only
distinguish two states (`RENDERED` vs `NOT SELECTED`) - `MemoryTurnTrace`
was never given enough information to also say whether a non-selected
candidate was dropped at dedup, ranking, or budget. This is not
fabricated; the funnel (Phase 2) gives the aggregate stage-by-stage
counts alongside it instead.

## 4. Retrieval Funnel (Phase 2)

`collect_retrieval_funnel(modules, turn_id="", conversation_id="")`
returns an ordered list of `{stage, label, count}` for `query` ->
`topic_candidates` -> `memory_candidates` -> `context_items` ->
`after_dedup` -> `after_ranking` -> `after_budget` -> `prompt`, straight
from `MemoryTurnTrace.funnel` (itself a copy of `assemble_context(funnel=
...)`'s own write-only output). A missing stage key is reported as
`None` ("not measured this turn" - e.g. the function returned early for
a signal-less query before later stages ran), never fabricated as `0`.
`tests/test_memory_voice_observability.py::test_c_funnel_stages_present_and_ordered_sanely`
asserts the funnel is monotonic non-increasing from `context_items`
through `prompt` and that `prompt == after_budget` - the concrete
"candidate -> ranking -> budget -> prompt" distinguishability Phase 2
required.

## 5. Topic History Timeline (Phase 3)

`collect_topic_history_timeline(modules, conversation_id=...)` walks
every turn for one conversation still present in the bounded
`_turn_trace_history` ring buffer, returning each turn's own captured
`active_topic_terms`/`topic_history` snapshot (taken at the START of
that turn, before that turn's own update), plus a final `"current"` entry
showing the LIVE `_active_topic`/`_topic_history` state right now.
Read-only by construction (only ever calls `.get()` on the live dicts) -
`test_d_topic_history_display_does_not_mutate_production_state` calls
every new collector twice in a row and asserts `_active_topic`/
`_topic_history` are unchanged before vs. after. `test_e`/`test_f`
reproduce the sprint brief's own worked example (ESP32/INMP441/mic turn,
unrelated aquascape turn, "Yang tadi soal mic gimana?") end-to-end
through the real production path and assert the CORRECT topic-history
entry is marked `referenced`/`produced_candidate` - and, symmetrically,
that an aquascape follow-up does NOT falsely mark the ESP32 entry as
referenced (contamination check). `test_g`/`test_l` prove two
conversations' (including two CONCURRENT conversations', via real
threads) topic histories never cross-contaminate.

## 6. Memory Quality Metrics (Phase 4)

`collect_memory_quality_metrics(modules)` computes real percentages/
averages/distributions over the turns still present in the bounded
`_turn_trace_history` sample (honestly labeled via `"sample_size"`, never
"all time"): retrieval hit rate, empty retrieval rate, topic continuity/
candidate hit rate, average candidate count, average rendered context-
item count, intent distribution, reference-type distribution, topic
switches, topic returns/references. Per the brief's own explicit
instruction, `topic_contamination_rate`, `average_prompt_context_size`,
and `budget_utilization` are returned as the literal string
`"Unavailable / telemetry not instrumented"` rather than estimated -
no token/byte size is ever captured anywhere in `MemoryTurnTrace` (only
item counts), and contamination would require ground truth this
telemetry has no way to establish without a second judge (explicitly
forbidden).

## 7. Voice Pipeline Metrics (Phase 6)

`collect_voice_pipeline(recorder, log_capture, request_id="")` returns,
for one turn (most recent if unspecified): `streaming_enabled`,
`pipelined_playback_enabled`, `cancelled`, `pause_count`/`resume_count`,
`chunk_count`, `llm_execution_time_ms` (straight from `LLMFinished`'s own
payload, already computed by the adapter), the five derived latencies
(`llm_first_token`, `llm_total`, `first_audio`, `playback_duration`,
`total_turn` - each a plain subtraction between two timestamps
`VoiceLatencyRecorder` already captured off real bus events), and a
per-chunk breakdown (`chunks`) parsed via `parse_chunk_timeline_from_logs()`
out of the dashboard's existing `LogCapture`. A missing endpoint (e.g. no
TTS ran this turn) yields `None` for that latency, never a fabricated
value. `collect_voice_latency_timeline(recorder, limit=None)` returns
the same per-turn breakdown across recent turns for Phase 7's timeline.

## 8. Latency Timeline (Phase 7)

The `/api/voice/latency_timeline` response is a flat list of per-turn
`{request_id, conversation_id, cancelled, pause_count, latencies_ms}`
records, rendered client-side (`static/index.html`'s "Latency Timeline"
subtab) as a table distinguishing LLM time from first-audio time from
total playback time per turn - directly answering "why does Luno feel
slow?" (LLM-bound vs TTS/playback-bound vs an inter-chunk gap) without
the dashboard guessing at a cause.

## 9. Privacy / Safety Boundaries

- No raw user/assistant text is ever stored in `MemoryTurnTrace` or
  `VoiceLatencyRecorder` (verified: `test_a_turn_trace_recorded_after_real_turn`
  asserts the raw query string does not appear anywhere in the trace
  JSON payload).
- No credentials/secrets/API keys/tokens are exposed by any new
  collector (none of the new fields touch adapter config).
- Every new collector is READ-ONLY: none of `collect_memory_turn_list`/
  `collect_memory_decision_trace`/`collect_retrieval_funnel`/
  `collect_topic_history_timeline`/`collect_memory_quality_metrics`/
  `collect_voice_pipeline`/`collect_voice_latency_timeline` ever calls
  `memory_retriever.retrieve_memories()`, ranks anything, or publishes an
  Event.
- `VoiceLatencyRecorder`/`_turn_trace_history` are both bounded
  (`maxlen=200`/`maxlen=100` respectively) - a long-running session
  cannot grow either without bound (`test_k_*` proves both under load
  exceeding their bound).
- Every new GET route added to `server.py` is read-only; zero new POST
  (control) routes were added by this sprint.
- No changes to the prompt-injection trust boundary
  (`docs/change_impact/memory_prompt_injection_rendering_boundary.md`'s
  own rendering path is untouched - the new telemetry reads
  `AssembledContext`/`MemoryTurnTrace` AFTER rendering already happened,
  never re-renders or re-injects anything).

## 10. Performance Overhead

- `assemble_context(funnel=...)`'s writes are `O(1)` dict assignments at
  points the function already reaches - no extra iteration, no extra
  retrieval call.
- `VoiceLatencyRecorder._on_event()` does a dict lookup + a few field
  assignments per already-published event, wrapped in `try/except: pass`,
  subscribed at `priority=-1000` (dead last - never delays real event
  delivery to other subscribers).
- `parse_chunk_timeline_from_logs()` only runs when the Voice Pipeline
  panel/endpoint is actually requested (not on every turn), and operates
  over an already-bounded `LogCapture.snapshot()` result, never the full
  log history.
- No new tokenizer, no new embeddings, no LLM/embedding judge, no second
  retrieval or ranking pass anywhere in this sprint's code - confirmed by
  source inspection of every new/modified function.

## 11. Tests

`tests/test_memory_voice_observability.py` - 17 tests, scenarios A-P per
the sprint's own Phase 10 checklist plus one production-path E2E test
(see `docs/testing/regression_baseline.md`'s dated entry for the full
scenario-by-scenario description). Notably: H/I call
`memory_context.assemble_context()` directly with and without `funnel=`
on identical inputs and assert the returned `AssembledContext` is
byte-for-byte identical either way - the strongest available proof that
the new telemetry parameter cannot influence ranking or retrieval. J
monkeypatches `build_turn_trace()` to raise and proves a real
conversation turn still completes normally. Stable across 3 consecutive
full runs of the file.

## 12. Regression Results

2121 tests collected across `tests/` (excluding the 2 permanently-
uncollectible files unrelated to this sandbox). Full sweep run across 13
batches (memory suite, response/voice/lifecycle suite,
`test_llm_tts_streaming_production.py`, `test_production_launcher.py`,
TTS/streaming/screen/semantic suite, vision/voice/wake/world_model suite,
camera suite, dashboard/desktop/emotion/episodic suite, incremental-
speech/LLM-dashboard/LLM-streaming suite, memory_conflict/intelligence/
maintenance/prompt_intelligence + `test_mic_device_index.py`,
persistent-state-hardening/proactive/real_adapters/relationship/
routing_dashboard/screen_intent/state_isolation/verification_dashboard
suite, `test_real_fish_audio_console.py`). Only the same 10,
already-documented, environment-specific failures remain (network
reachability, sandbox `.env`/missing script, missing native audio
libraries, sandbox `inspect.getsource()` gap - see
`docs/testing/regression_baseline.md`'s dated entry for the full list).
**Zero new regressions.**

## 13. Persistent-State Verification

All 14 `config/*.json` files hashed (SHA256) and mtime-checked
immediately before and immediately after the full regression sweep -
byte-identical, zero changes. No new or unexpected files were created.
`_turn_trace_history` and `VoiceLatencyRecorder`'s internal state are
purely in-memory (verified by source inspection - neither file contains
an `open()`/file-write call), never wired to any `luno/config.py` path,
never dumped to disk.

## 14. Known Limitations

- Per-candidate status in the Memory Decision Trace is limited to
  `RENDERED`/`NOT SELECTED` - it cannot say whether a non-selected
  candidate was dropped at dedup, ranking, or budget specifically (see
  §3 above). The aggregate funnel (§4) is the mitigation.
- `topic_contamination_rate`, `average_prompt_context_size`, and
  `budget_utilization` are honestly reported as
  `"Unavailable / telemetry not instrumented"` rather than estimated.
- Voice per-chunk detail (`chunks` in `collect_voice_pipeline()`) is only
  available for the window still present in the bounded `LogCapture`
  ring buffer (`DEFAULT_LOG_BUFFER_SIZE=10000` lines) - an old request_id
  whose log lines have already rotated out returns an empty `chunks`
  list, not fabricated data.
- `_turn_trace_history` only holds the most recent 100 turns across every
  conversation combined (not per-conversation) - a long-running,
  multi-conversation session's oldest turns will fall out of the Memory
  Debug Inspector's turn picker first.

---

## Final Report

**STATUS:** Complete.

**FILES CREATED:**
- `luno/dashboard/voice_latency.py`
- `tests/test_memory_voice_observability.py`
- `docs/change_impact/memory_voice_observability_dashboard.md` (this file)

**FILES MODIFIED:**
- `luno/memory_context.py` (`assemble_context(funnel=...)` - additive)
- `luno/memory_turn_trace.py` (additive `MemoryTurnTrace` fields +
  `build_turn_trace()` kwargs)
- `main_runtime_demo.py` (`PlannerBridgeModule._turn_trace_history` +
  call-site wiring)
- `luno/dashboard/collectors.py` (new Phase 1-4/6-7 collector section)
- `luno/dashboard/server.py` (new GET routes + `VoiceLatencyRecorder`
  lifecycle)
- `luno/dashboard/static/index.html` (new "Brain Debugger" nav panel)
- `ARCHITECTURE_GUARD.md` (new §32)
- `docs/testing/regression_baseline.md` (new dated entry)

**PHASE 0 FINDINGS:** See §1 above.

**DASHBOARD FEATURES ADDED:** Memory Decision Trace, Retrieval Funnel,
Topic History Timeline, Memory Quality Metrics, Memory Debug Inspector
(turn picker + trace, folded into the Decision Trace subtab), Voice
Pipeline Observability, Latency Timeline - one new "Brain Debugger" nav
group with 6 subtabs in `static/index.html`.

**MEMORY TELEMETRY:** `assemble_context(funnel=...)`, `MemoryTurnTrace`
extension, `PlannerBridgeModule._turn_trace_history` - see §2-6.

**VOICE TELEMETRY:** `VoiceLatencyRecorder`, `parse_chunk_timeline_from_logs()` -
see §7-8.

**TEST RESULTS:** `tests/test_memory_voice_observability.py` 17/17,
stable across 3 consecutive full runs.

**E2E RESULTS:** `test_z_e2e_real_production_path_through_dashboard_http`
passes - `RuntimeDemoConsole` -> `PlannerBridgeModule` -> memory/context
pipeline -> telemetry -> a real, running `DashboardServer`'s HTTP API,
verified over real HTTP via `requests`.

**FULL REGRESSION RESULTS:** 2121 tests collected, only the same 10
pre-existing, already-documented environment-specific failures. Zero new
regressions. See §12.

**PERSISTENT STATE VERIFICATION:** All 14 `config/*.json` files
SHA256+mtime byte-identical before/after the full sweep. No new files.
See §13.

**PERFORMANCE OVERHEAD:** O(1) dict writes at already-reached points;
passive `priority=-1000` Event Bus subscriber; log parsing only runs
on-demand. No new tokenizer/embeddings/LLM judge/second retrieval or
ranking pass. See §10.

**KNOWN LIMITATIONS:** See §14.

**INVARIANTS VERIFIED:**
- memory decision logic changed: **NO**
- memory retrieval logic changed: **NO**
- memory ranking changed: **NO**
- topic-selection logic changed: **NO**
- response selection changed: **NO**
- TTS behavior changed: **NO**
- streaming behavior changed: **NO**
- persistent raw topic state added: **NO**
- second ranking system added: **NO**
- LLM/embedding judge added: **NO**
