# Sprint 57 — Contextual Home Assistant References & Target Continuity

**Status:** COMPLETE for the contextual-reference implementation, hardening,
message-quality fix, testing, and targeted+full regression verification.
`config/long_term_memory.json`'s corrupted/unknown-format state is
diagnosed but explicitly DEFERRED (not fixed) — see the dedicated section
near the end of this document.

## Phase 0 — Reconnaissance (mandatory, read-only)

Read, in order: `docs/project_handover.md`, `docs/project_handover.json`,
`docs/change_impact/sprint56_ha_query_intelligence.md`,
`docs/change_impact/sprint55_stability_gate.md`, `ARCHITECTURE_GUARD.md`
§56/§57 (Sprint 55/56's own sections), then the actual source: HA command
parsing/resolution (`luno/planner/parser.py`'s `IntentParser`), `luno/
tool_manager/builtin/real_home_assistant.py`, `luno/memory_context.py`,
the Event Bus (`luno/core/event_bus.py`), `SessionManagerModule` (`luno/
wake_session/manager.py`), conversation/turn state in `main_runtime_demo.py`,
Sprint 50's observability conventions, Sprint 52's resolver, Sprint 56's
differentiator.

### A — Current HA command flow

`IntentParser.parse(text)` (regex-based, `luno/planner/parser.py`) turns
raw text into `ParsedStep(tool="home_assistant", action, target, params)`
objects for `turn_on`/`turn_off`/`set_color`/`set_brightness`/`set_value`/
`run_script`. The Planner's `TaskExecutor` calls `PlannerBridgeModule.
_tool_bridge_handler()` (registered once per tool at `__init__`), which
publishes `tool_requested` on the Event Bus, blocks on a `threading.Event`,
and returns `ToolFinished.data` or raises with `ToolFailed.data` attached.
`ToolManagerBridgeModule` (single-worker `ThreadPoolExecutor`) dispatches
to `RealHomeAssistantHandler.execute()`.

### B — Current entity resolution flow

`execute()` resolves `tool_call.target` via `_resolve_entity_tiered()`
(Sprint 52): exact name → alias → literal `entity_id` → bounded fuzzy
(`difflib.SequenceMatcher`, gated by confidence+margin, refuses on a
near-tie) → unknown/refuse. Sprint 56 re-verified this resolver unmodified
and added `_narrow_by_query_differentiator()` to `luno/memory_context.py`'s
`select_topic_candidates()` — a SEPARATE subsystem, scoped to LLM prompt
context injection, not the deterministic HA execution path.

### C — Existing conversation/context state safely reusable

`PlannerBridgeModule._apply_device_context(text, conversation_id)` — the
key finding of this sprint's own Phase 0 (see below) — a live, pre-existing,
tested (`tests/test_device_context.py`, 20 tests before this sprint)
text-rewrite mechanism that already REMEMBERs the last real `home_
assistant` target per conversation and FILLs a later target-less
single-clause command from it, before `IntentParser` ever runs. Also
reusable: `ConversationEnded` (real, published by `SessionManagerModule`,
handled by `PlannerBridgeModule._on_conversation_ended`) as the genuine
context-reset boundary — `conversation_reset` (a different event) is
DEAD CODE, defined and routed but never published anywhere in this
codebase, ruled out as the reset trigger.

### D — Why contextual references were previously deferred

Sprint 56's Phase 13 investigated exactly two layers: the Tool Manager
resolver (`real_home_assistant.py` — deterministic, execution-time, has
no "last target" concept) and `memory_context.py`'s topic machinery
(LLM-prompt-context only, not wired to HA execution). Correctly found
neither suitable, and correctly refused to build a THIRD, brand-new
state system "from scratch" without a full design pass (the brief's own
explicit warning). What Sprint 56 did not investigate: `main_runtime_
demo.py`'s own `PlannerBridgeModule._apply_device_context()` — a
text-rewrite layer that predates Sprint 52/55/56 entirely and already
solves exactly this problem, just without freshness, domain-compatibility,
or failure-invalidation hardening.

### E — Which existing state supports contextual references without a second system

`PlannerBridgeModule._last_device_target: Dict[conversation_id, Dict[tool,
value]]` — already conversation-scoped, already reset on `ConversationEnded`,
already bounded (`_last_device_target_max = 50`). This sprint enriches the
per-tool VALUE (bare string → `{target, turn_seq, entity_id, domain}`) and
adds one sibling dict (`_device_context_turn_seq`, same lifecycle) for
freshness — no second memory/topic system, no new conversation-keyed
top-level structure.

### F — Exact files/functions modified

`main_runtime_demo.py`: `PlannerBridgeModule.__init__` (new attributes),
`_apply_device_context()` (rewritten body), `_tool_bridge_handler()` (2
new call sites), `_handle_utterance()` (1 new line), `_on_conversation_
ended()` (1 new line), plus new methods `_device_context_entity_info()`,
`_remember_device_target()`, `_invalidate_device_context_on_failure()`,
and new/extended constants (`_CONTEXT_REMEMBER_ACTIONS`, `_CONTEXT_MAX_
TURN_AGE`, `_CONTEXT_FILL_COMPATIBLE_DOMAINS`, `_CONTEXT_FILLER_WORDS`
extended with "yang"/"tadi"). `luno/tool_manager/builtin/real_home_
assistant.py`: `execute()` (1 new guard) + new `_missing_target_result()`
method.

### G — Exact files/functions that must remain protected

`RealHomeAssistantHandler._resolve_entity_tiered()`/`_score_candidates()`
(Sprint 52's resolver — untouched, re-verified), `luno/memory_context.py`'s
`_narrow_by_query_differentiator()`/`select_topic_candidates()` (Sprint 56
— untouched, re-verified), memory ranking/persistence (`luno/memory.py`,
`luno/memory_guard.py`), planner architecture (`luno/planner/executor.py`,
`luno/planner/models.py`), LLM routing (`luno/routing/`), TTS/Fish Audio/
Whisper/Vision adapters, dashboard turn-state recovery, the Event Bus
architecture itself (`luno/core/event_bus.py` — only a new event TYPE was
published via the existing API, no core change), WLED/device integrations,
existing HA tool execution semantics (verification loop, retry/backoff).

## Design — the contextual resolution algorithm

Every call to `_apply_device_context(text, conversation_id)`:

1. **Turn counter.** `current_turn_seq = _device_context_turn_seq[key] + 1`
   (bumped unconditionally, every call — including turns that name no
   device and turns for tools other than `home_assistant`). This is what
   "turn age" freshness is measured against.
2. **REMEMBER.** Collect every DISTINCT target across all parsed steps
   where `tool == "home_assistant"`, `action` is in the broadened
   `_CONTEXT_REMEMBER_ACTIONS = {turn_on, turn_off, set_color, set_
   brightness, set_value}`, and the target is a real, configured device
   (`_is_known_home_assistant_device`). Exactly one distinct target →
   `_remember_device_target()` writes `{target, turn_seq: current_turn_
   seq, entity_id, domain}` (entity_id/domain resolved via `_device_
   context_entity_info()`, same lookup shape as the pre-existing
   known-device check). Two or more distinct targets in the SAME turn →
   the existing memory is CLEARED (ambiguous evidence about "the device
   this conversation is about" — never resolved by picking whichever
   clause was last). Zero targets → existing memory left exactly as-is.
3. **FILL.** Only when `len(steps) == 1` and that step is `home_assistant`/
   `turn_on` or `turn_off` (unchanged — the FILLABLE set stayed narrow;
   only REMEMBER broadened) with no real target (empty, or pure filler
   per `_CONTEXT_FILLER_WORDS`). If a device is remembered, three gates
   must ALL pass before it is used:
   - **Freshness:** `0 <= (current_turn_seq - remembered.turn_seq) <=
     _CONTEXT_MAX_TURN_AGE` (6 turns).
   - **Domain compatibility:** `remembered.domain in {light, switch, fan,
     climate, media_player}` — HA's own generic `homeassistant.turn_on/
     turn_off` services' real supported domains.
   - (Implicit) the memory must exist at all (`isinstance(remembered,
     dict)`).
   All three pass → rewrite to the canonical `"turn {on|off} {target}"`
   text, which re-parses and re-executes identically to any explicit
   command (same Planner → verified-execution → honest-result path). Any
   gate fails → `text` returned unchanged, falling through to the
   (now-fixed) honest "which device did you mean" refusal.
4. **Observability.** Exactly when step 3's "no real target" branch is
   reached (an attempt was made, regardless of outcome), one structured
   `device_context_resolution` event publishes (if an Event Bus is
   bound) — see the Observability section below.

## Ambiguity policy

Never guesses. Two distinct real devices named in ONE turn → memory
cleared, not resolved. A tied cross-turn "candidate A vs. candidate B"
shape cannot arise from this single-slot "last device" design (there is
only ever one remembered value per tool) — the closest real-world
analogue, a typo'd EXPLICIT target that's textually near two different
real devices, is Sprint 52's own territory (fuzzy resolver's margin gate,
re-verified unmodified) and is never reached by the contextual layer at
all, since an explicit (even if typo'd/unresolvable) target always skips
the FILL branch entirely (`no_real_target` is false for a genuine,
if-wrong, device-shaped string).

## Freshness policy

`_CONTEXT_MAX_TURN_AGE = 6`, measured via the new `_device_context_turn_
seq` per-conversation counter (bumped every call, independent of whether
that call named a device). Chosen to match `memory_context.py`'s own
`_ACTIVE_TOPIC_MAX_AGE_TURNS` — same bounded-turn-count SHAPE, verified
NOT to be shared/coupled state (two separate module-level constants, two
separate subsystems, confirmed by `test_V_device_context_and_query_
differentiator_do_not_share_or_corrupt_state`). Boundary-tested: age ==
6 still fills; age == 7 refuses (`test_S_context_expiration_boundary_*`).
`ConversationEnded` resets both `_last_device_target` and `_device_
context_turn_seq` for that conversation — a new conversation never
inherits stale turn-age arithmetic even if it happens to reuse a
`session_id`.

## Failed-command policy

A command's target is optimistically remembered at PARSE time (before
the real HA execution result is known — REMEMBER runs synchronously
inside `_apply_device_context`, upstream of the Planner). `_invalidate_
device_context_on_failure(tool_call)`, called from `_tool_bridge_
handler()`'s failure branch (both the `tool_failed` case and the timeout
case), corrects that optimism: if the call that just failed was `home_
assistant` and its target matches what this conversation currently has
remembered, the memory is popped. Scoped to conversation identity via a
`threading.local()` slot (`_tool_bridge_local.conversation_id`, set once
per spawned `luno-planner-turn` thread at the top of `_handle_
utterance()`) — neither `luno.planner.ToolCall` nor `luno.tool_manager.
ToolCall` carries a `conversation_id` field, and `_tool_bridge_handler`
is one shared-instance method with no per-call conversation parameter, so
a bare instance attribute would race under concurrent per-utterance
threads (confirmed via `on_event()`'s own "spawns a fresh thread per
utterance" design). Best-effort and fail-closed: a missing/unset
thread-local (e.g. a direct unit test of `_tool_bridge_handler` alone) is
treated as "nothing to invalidate", and the invalidation helper itself
can never raise (it must never mask the real failure `_tool_bridge_
handler` is about to propagate).

Separately, AMBIGUOUS commands never even reach this mechanism: the raw,
unresolved, typo'd text of an ambiguous command (e.g. "rgb cprip") is not
a recognized device name, so REMEMBER's own `_is_known_home_assistant_
device()` guard already excludes it from ever being written to memory in
the first place — independent of, and simpler than, the failure-
invalidation path.

## Domain compatibility

`_CONTEXT_FILL_COMPATIBLE_DOMAINS = {light, switch, fan, climate,
media_player}` — the real HA domains `homeassistant.turn_on`/`turn_off`
generically support. A remembered device whose domain falls outside this
set (e.g. `lock`, `vacuum`, `cover`) is never used to fill a plain on/off,
even if fresh. This checkout's real registry only configures light/switch
devices, so `fan`/`climate`/`media_player`/incompatible-domain coverage
is proven via engineered fixtures that directly construct a `_last_
device_target` entry with the domain under test (mirrors Sprint 52's own
`test_T` "no natural example exists, prove the gate anyway" precedent).

## Observability

One new structured Event Bus event, `device_context_resolution`,
published only when a contextual resolution is ATTEMPTED (the FILL
branch's "no real target" case is reached) — never for explicit commands.
Same pattern as Sprint 50's own `memory_reference_classified` (`self.
_event_bus.publish(Event(type=..., data={...}))`, guarded by a bare
`if self._event_bus is not None`, wrapped in try/except so telemetry can
never break a turn). Fields:

| field | meaning |
|---|---|
| `conversation_id` | the conversation this resolution belongs to |
| `attempted` | always `True` when this event fires |
| `resolved` | whether a target was actually filled in |
| `candidate_count` | `0` or `1` — single-slot memory, not a ranked list |
| `target` | the resolved device slug (never raw text), or `None` |
| `refusal_reason` | `"no_memory"` / `"stale"` / `"incompatible_domain"` / `None` |
| `turn_age` | turns since the remembered device was last set, or `None` |

No raw user utterance is ever included, matching `memory_reference_
classified`'s own long-standing "never raw conversation text" boundary.

## Message-quality fix (`real_home_assistant.py`)

Root cause: `execute()`'s guard `if target and entity_id is None: return
self._unknown_device_result(tool_call, target)` only fires when `target`
is truthy. A genuinely target-less command (`target` empty/`None` — e.g.
a bare "Matikan." that context had nothing fresh/compatible to fill)
fell through into `_execute_on_off`/the `set_color`/`set_brightness`
branches with `entity_id=None` AND `target=""`, where `friendly = target
or entity_id` evaluates to `None`, eventually producing `"None is
currently unavailable."` — confusing, and structurally different from
the honest "I couldn't find '<name>'" refusal a genuinely unrecognized
device name gets.

Fix: a second, narrower guard — `if entity_id is None and not target and
tool_call.action != "run_script": return self._missing_target_result(
tool_call)` — and a new, dedicated refusal method (`_missing_target_
result`, `error_type="MissingTarget"`, message: `"Which device did you
mean? I don't have one to go on right now."`). Kept SEPARATE from `_
unknown_device_result` (which means "you named a device but I don't
recognize it" and offers fuzzy suggestions) since there is no misspelled
name to suggest against in the missing-target case. `run_script`'s own
pre-existing no-target fallback (`script_entity = entity_id or (target or
tool_call.parameters.get("script"))`) is explicitly exempted — verified
via `test_run_script_with_no_target_keeps_its_own_pre_existing_
behavior`. A genuinely resolved-but-physically-unavailable device (a real
`entity_id` that reports "unavailable") still gets the ORIGINAL, unrelated
"X is currently unavailable" message unchanged — verified directly
(`test_G_device_unavailable_after_context_fill_gives_normal_unavailable_
message_not_missing_target`).

## Explicit-target priority (verified, unchanged)

Precedence is architectural, not a single ordered check: `_apply_device_
context`'s FILL branch is gated on `len(steps) == 1 AND no_real_target`.
Any utterance that names a real-looking target — whether it resolves
(exact/alias/Sprint-52-fuzzy) or not (a typo, an unrecognized name) —
has `no_real_target = False`, so it is passed through completely
untouched to the normal Planner → `IntentParser` → `execute()` →
Sprint-52-resolver path, with the contextual layer never even considered.
Verified directly: an explicit target after a different remembered
device (test A), a similar-but-different-named explicit target (test I),
a typo'd explicit target with a different device fresh in memory (test J,
plus full end-to-end proof the typo still resolves via the unmodified
Sprint 52 fuzzy tier), an explicit target with trailing referential
words in the SAME clause ("rgb strip yang itu", test K), and a dedicated
"explicit always beats fresh contextual memory" test.

## Safety matrix (A–V) — all implemented as dedicated tests

`tests/test_sprint57_contextual_ha_references.py` (42 tests):

| scenario | test(s) |
|---|---|
| A — explicit target after previous target | `test_A_*` |
| B — one clear contextual target | `test_B_*` (x2: plain on/off, `set_color`-sourced) |
| C — two possible contextual targets (same turn) | `test_C_*` |
| D — no contextual target | `test_D_*` |
| E — stale contextual target | `test_E_*` |
| F — context after conversation reset | `test_F_*` |
| G — device unavailable | `test_G_*` |
| H — previous target, different HA domain | `test_H_*` (x2: incompatible + compatible-but-unconfigured) |
| I — previous target, similar name | `test_I_*` |
| J — typo in explicit target | `test_J_*` (x2: passthrough + real fuzzy resolution) |
| K — explicit target + contextual phrase | `test_K_*` |
| L — multiple commands in sequence | `test_L_*` |
| M — context after unrelated conversation | `test_M_*` |
| N — context after non-HA conversation | `test_N_*` |
| O — context after failed HA command | `test_O_*` (5 tests: helper-level, scoping, exception-safety, full Event-Bus end-to-end, tool-type scoping) |
| P — context after successful HA command | `test_P_*` |
| Q — context after ambiguous HA command | `test_Q_*` |
| R — repeated contextual commands | `test_R_*` |
| S — context expiration (boundary) | `test_S_*` (x2: exact limit, one past) |
| T — "yang itu"/"yang tadi"/"-nya" | `test_T_*` (x4) |
| U — context after multiple different devices (sequential) | `test_U_*` |
| V — query-side differentiator + contextual history | `test_V_*` |

Plus: explicit-priority-with-fresh-memory-present, missing-target
message quality (x3), performance (<5ms), no-LLM/network import
(structural), and 3 observability tests. `tests/test_device_context.py`
(22 tests, 2 pre-existing shape assertions updated, 0 behavior changes to
the other 20) re-verifies the pre-existing REMEMBER/FILL/isolation/
camera-follow-up contract is fully preserved, including the full
end-to-end `RuntimeDemoConsole` test.

### Requirement-list cross-check (the brief's own numbered list)

1. Normal explicit HA commands unchanged — test A + full `test_device_
   context.py` suite.
2. Sprint 52 fuzzy resolution unchanged — `tests/test_sprint52_ha_
   entity_resolution.py` (35 tests, re-run, 0 failed) + test J.
3. Sprint 56 differentiator unchanged — `tests/test_sprint56_query_
   entity_differentiator.py` (re-run, 0 failed) + test V.
4. Contextual resolution works with exactly one valid target — test B.
5. Ambiguous context refuses safely — test C.
6. Stale context does not resolve — test E/S.
7. Failed commands don't poison context — test O.
8. Explicit target always beats contextual — test A/I/J/K + dedicated test.
9. Conversation/session reset clears contextual state — test F.
10. Existing HA safety tests continue passing — `tests/test_sprint52_
    ha_entity_resolution.py` + `tests/test_sprint56_ha_safety_matrix.py`,
    both re-run, 0 failed.

## Regression results

**Targeted** (this file + `test_device_context.py` + Sprint 52 HA +
Sprint 56 HA safety matrix + Sprint 56 differentiator + `test_memory_
context.py` + `test_dashboard_turn_state_recovery.py` x2 + `test_wake_
session_console.py` + `test_conversation_ended_lifecycle_routing.py` +
`test_response_policy.py` + `test_runtime_demo.py`): **337 passed, 0
failed.**

**Full repository sweep** (`python3 -m pytest tests/ -q --ignore=tests/
test_main_bargein.py --ignore=tests/test_root_main_bargein.py -n 4
--dist loadfile --timeout=90`, same 2-file exclusion convention every
prior sprint's baseline uses — those 2 files are permanently
uncollectible in this sandbox, missing `faster_whisper`): **3079 passed,
11 failed, 3 skipped** (3093 collected). All 11 failures individually
re-run in isolation:

- **3 timing-window flakes under parallel CPU contention — PASS
  standalone:** `test_llm_tts_streaming_production.py::test_13_
  cancellation_before_first_audio`, `test_state_isolation.py::test_
  planner_turn_thread_can_genuinely_outlive_console_stop`, `test_
  streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_
  plays` (this last one is the EXACT, already-documented scheduling-
  jitter flake class named in `docs/testing/regression_baseline.md`
  across essentially every prior sprint's regression run).
- **8 environment-specific — this checkout's real `.env`/hardware
  differs from what the test assumes, same class documented in `docs/
  testing/regression_baseline.md` for every prior sprint:**
  `test_mic_device_index.py` (4 tests — real `.env` sets
  `MIC_DEVICE_INDEX=1`), `test_production_launcher.py::test_07_health_
  checks_all_pass_in_default_mock_configuration` (real live credentials
  configured), `test_real_adapters.py` (2 tests — `faster_whisper`
  absent from this sandbox), and one NEW instance of the identical
  class: `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_
  state` (asserts `current_provider == "openai"`; this checkout's real
  `.env` sets `LLM_PROVIDER=openrouter`).

None of the 11 touch `main_runtime_demo.py`'s `PlannerBridgeModule`,
`real_home_assistant.py`, `tests/test_device_context.py`, or `tests/
test_sprint57_contextual_ha_references.py`. **Zero genuine regressions.**

## Performance

`_apply_device_context()`: ~0.02ms/call average (1000-call measurement,
mixed fresh/stale calls). Far under the 5ms budget. No LLM call, no
network call, no embeddings — verified both structurally (a source-scan
test asserts none of the new methods reference `openai`/`openrouter.
chat`/`requests.`/`httpx.`/`embedding`) and by design (the entire
contextual path is dict lookups, `_slugify()` string comparisons, and
one `luno.devices` registry scan — no `difflib`, no network I/O).

## Persistent state verification

`find config -type f | xargs md5sum` captured before and after both the
targeted regression run and the full repository sweep — byte-identical
(zero drift) in both cases, across every `config/*.json` file and the
`vision_memory.sqlite3`/`-shm`/`-wal` files. This sprint's own source
changes touch no config file at all (the only state mutated at runtime is
in-memory, per-`PlannerBridgeModule`-instance dicts).

## `config/long_term_memory.json` — diagnosed, deferred

Flagged by Sprint 55/56 as failing to load (`'utf-8' codec can't decode
byte 0x9c in position 4: invalid start byte`). This sprint's own
diagnosis, performed strictly separately from the HA implementation (per
the brief's own explicit instruction):

- File: 1849 bytes, permission `r--r--r--` (read-only).
- Not valid JSON (the loader, `luno/memory.py`, uses plain `json.load()`).
- Not gzip (`1f 8b` magic bytes absent).
- Not standard zlib (`78 9c`/`78 01`/`78 da` header absent, checked at
  every byte offset in the first 16 bytes).
- Not any common text encoding: UTF-8 and UTF-16 both fail to decode;
  Latin-1 "succeeds" (Latin-1 never rejects any byte) but produces
  meaningless mojibake, not readable text.
- Shannon entropy: 7.65 bits/byte (theoretical max 8.0) — consistent with
  encrypted or compressed data, inconsistent with ordinary corrupted-but-
  mostly-readable text (which would show markedly lower entropy from
  surviving ASCII structure).
- No backup exists for this specific file — `config/backups/` contains
  only `relationship_state.*.json` backups, a different, unrelated file.

**Conclusion: format/root cause UNKNOWN.** Not clearly safe to fix (no
identifiable encoding/encryption scheme to reverse, no backup to restore
from, and the file's own read-only permission bit is a plausible
deliberate protection against further data loss that this sprint should
not override unilaterally). Also out of scope for a contextual-HA sprint
regardless of cause. The EXISTING load path already fails closed and
safely — `luno/memory.py` catches the decode error, tries the (absent)
backup, and falls back to an empty long-term memory store with a clear
console warning; the system is not broken, it simply operates with no
long-term memory loaded. **Deferred explicitly** to a dedicated future
investigation. Recommendation: check whether the original `E:\Luno Evo`
device (or wherever this checkout was exported from) has an out-of-band
backup/export of this file predating whatever produced the current
content, since nothing in this sandbox can recover it.

## Known limitations

- Cross-turn ambiguity between two DIFFERENT remembered devices cannot
  arise by construction (single-slot "last device" memory holds only
  one value per tool) — this is a deliberate simplicity/safety trade-off,
  not a gap: the closest real-world equivalent (a target string that's
  textually close to two different devices) is already Sprint 52's own
  territory and is handled by its fuzzy resolver's margin gate, untouched
  by this sprint.
- `domain` compatibility for `fan`/`climate`/`media_player` is proven via
  engineered fixtures only — this checkout's real device registry has no
  natural example of either domain to exercise the gate end-to-end. The
  gate is domain-based (not registry-based), so this does not weaken the
  guarantee, only the naturalness of its test coverage.
- The `device_context_resolution` observability event's `candidate_
  count` field is always 0 or 1 by design (matches the single-slot memory
  shape) — it does not report a ranked list of alternatives the way
  Sprint 52's own `EntityResolutionDecision` event can for fuzzy matches.

## Deferred work

- `config/long_term_memory.json` corruption/format investigation (see
  dedicated section above) — needs an out-of-band recovery source this
  sandbox does not have access to.
- No other work from this sprint's own brief was deferred.

## Next recommended sprint

With Sprint 52 (entity resolution), Sprint 56 (query-side differentiator),
and Sprint 57 (contextual references) all now covering the HA command
surface, the next highest-value HA-adjacent gap is likely **multi-device
group commands** ("nyalain semua lampu" / "turn off everything in the
bedroom") — currently unimplemented and outside every prior sprint's
scope; or, as a smaller/faster win, revisiting the deferred `config/
long_term_memory.json` recovery if an out-of-band backup can be located
on the real device.
