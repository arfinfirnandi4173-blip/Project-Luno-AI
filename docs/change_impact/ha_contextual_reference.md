# Sprint 57 — Contextual Home Assistant References

**Status:** COMPLETE. Implements safe contextual HA device references
("Nyalain lampu kamar." → "Matikan.") by hardening a pre-existing,
already-live text-rewrite mechanism rather than building any new memory,
topic, or resolver system.

## 1. Root cause / problem

Before this sprint, a follow-up command that omitted the device name
(e.g. a bare "Matikan.") had no way to recover which device the
conversation had just been talking about. `IntentParser` produced
`target=None`, which failed safely (no device was ever wrongly
activated) but unhelpfully — the user had to repeat the device name
every time.

## 2. Existing architecture reused

Reconnaissance (Phase 0) found that `PlannerBridgeModule._apply_device_
context()` in `main_runtime_demo.py` — a pre-existing, live, already-
tested mechanism that predates this sprint — already REMEMBERs the last
real `home_assistant` target named in a conversation and FILLs a later
target-less single-clause on/off command from it, before `IntentParser`
even runs. This sprint hardens that exact mechanism:

- **Where an HA entity becomes resolved:** `RealHomeAssistantHandler.
  _resolve_entity_tiered()` (Sprint 52, `real_home_assistant.py`) —
  untouched, still the sole resolver.
- **Where HA command execution succeeds or fails:** `PlannerBridgeModule.
  _tool_bridge_handler()` — publishes `tool_requested`, blocks for
  `tool_finished`/`tool_failed`.
- **Where existing turn/session context safely retains the resolved
  target:** `PlannerBridgeModule._last_device_target` — already
  conversation-scoped, already reset on `ConversationEnded`, already
  bounded. This sprint enriches its per-tool VALUE (bare string →
  `{target, turn_seq, entity_id, domain}`) rather than adding a second
  structure.
- **Sprint 50's Event Bus** — reused directly for the one new
  observability event this sprint adds (see §10).

No second memory system, no second topic tracker, no global mutable
`last_entity`, no independent HA resolver, and no parallel state
machine were created. `luno/memory_context.py`'s topic/differentiator
machinery (Sprint 49/56) is a wholly separate subsystem (LLM prompt
context, not HA execution) and was not touched or duplicated.

## 3. Resolution priority

```
1. Explicit entity / explicit target        <- IntentParser's own captured target
2. Sprint 52 exact/alias resolution          <- _resolve_entity_tiered()
3. Sprint 52 fuzzy resolution                <- _resolve_entity_tiered()
4. Sprint 56 query-side differentiator       <- _narrow_by_query_differentiator()
5. Contextual HA reference (this sprint)     <- _apply_device_context()
6. Safe refusal                              <- _missing_target_result() / _unknown_device_result()
```

Enforced structurally, not by a single ordered check: `_apply_device_
context()`'s FILL branch is gated on `len(steps) == 1 AND no_real_
target`. Any utterance carrying a real-looking target — resolved or
not — has `no_real_target = False` and passes through completely
untouched to the normal resolver chain (tiers 1–4), with the contextual
layer never even considered. Verified directly (test B, test N, and
`tests/test_sprint57_contextual_ha_references.py`'s own explicit-
priority tests).

## 4. Context lifetime

Chosen policy: **bounded turn-count freshness**, not a fixed wall-clock
timer, not "until an explicit topic change," and not indefinite
retention. A new per-conversation monotonic turn counter
(`_device_context_turn_seq`, bumped once per `_apply_device_context()`
call — including turns that name no device at all) measures "how many
turns old" a remembered device is; a FILL only succeeds when that age
is `<= _CONTEXT_MAX_TURN_AGE` (6 turns). This was chosen because:

- The existing architecture already has a turn-counting precedent
  (`memory_context.py`'s own `_ACTIVE_TOPIC_MAX_AGE_TURNS`, same value,
  confirmed NOT shared/coupled state — two independent constants).
- A fixed wall-clock timer would be non-deterministic and hard to test;
  a turn count is deterministic and matches how conversations actually
  advance.
- "Until an explicit topic change" would require detecting topic
  change — a new classifier this sprint's own hard invariants forbid
  building.
- Indefinite retention is unsafe (an unrelated command hours later
  could silently reactivate a stale device).

`ConversationEnded` (the real, live event `SessionManagerModule`
publishes — `conversation_reset` was checked and confirmed to be dead
code, never published anywhere in this codebase) resets both `_last_
device_target` and `_device_context_turn_seq` for that conversation, so
a new conversation never inherits stale turn-age arithmetic.

Non-HA interruptions (e.g. "Berapa suhu sekarang?") do NOT by themselves
invalidate context — they still count as one turn toward the freshness
budget (turn counter still increments), but the remembered device stays
valid until that budget is exhausted. This matches the brief's own
example ("Matikan." after "Nyalain lampu kamar." then "Berapa suhu
sekarang?" should not be silently assumed valid FOREVER, but is not
instantly invalidated either) — verified directly (test G).

## 5. Context creation rules

A target is only remembered from a clause that:

1. Has `tool == "home_assistant"`.
2. Has an action in the broadened remember set (`turn_on`, `turn_off`,
   `set_color`, `set_brightness`, `set_value` — NOT `run_script`, which
   has no fillable phrasing to begin with).
3. Names a target that resolves to an ACTUALLY configured device
   (`_is_known_home_assistant_device()` — the same registry check the
   pre-existing mechanism already used).

Never recorded from: a failed parse, a fuzzy candidate that wasn't
actually executed, an ambiguous candidate, or an unknown device name.
Two or more DISTINCT real devices named in the SAME turn clear the
existing memory rather than picking one (same-turn ambiguity, never
guessed).

**Failure invalidation (a stronger guarantee than "never record from a
failure"):** because REMEMBER runs at PARSE time (before the real HA
execution result is known), a device is recorded optimistically the
moment its name is recognized — then corrected if execution actually
fails. `_invalidate_device_context_on_failure()`, wired into `_tool_
bridge_handler()`'s failure AND timeout branches, un-remembers a target
if the tool call that just failed was `home_assistant` for that exact
target. Correlated to the right conversation via a `threading.local()`
slot (`_tool_bridge_local.conversation_id`, set once per spawned
`luno-planner-turn` thread) since neither `luno.planner.ToolCall` nor
`luno.tool_manager.ToolCall` carries a `conversation_id` field, and
`_tool_bridge_handler` is one shared-instance method with no per-call
conversation parameter — a bare instance attribute would race under
concurrent per-utterance threads.

## 6. Explicit-vs-context precedence

See §3. An explicit target — even a typo'd or unrecognized one — always
takes the non-contextual path. Proven with: an explicit device
immediately after a different remembered device (test B); a third bare
command correctly following the NEWEST explicit target, not the
original one (test N); a typo'd explicit target that is NOT overridden
by a different fresh contextual memory (`tests/test_sprint57_
contextual_ha_references.py::test_J_*`).

## 7. Ambiguity behavior

Never guesses. This is a single-slot "last device" memory (one value
per tool, not a ranked candidate list), so the only shape "multiple
equally valid candidates" can take is two distinct real devices named
in the SAME turn — handled by clearing memory rather than resolving
(test D). A tied cross-turn "candidate A vs. candidate B" shape cannot
arise by this design; the closest real-world equivalent (a target
string textually close to two different devices) is Sprint 52's own
territory (fuzzy resolver's margin gate) and is reached via the normal
explicit-target path, never the contextual layer (verified unchanged,
test L).

## 8. Capability validation

Domain compatibility gates the one context-fillable action family
(plain on/off): a remembered device's HA domain must be in
`_CONTEXT_FILL_COMPATIBLE_DOMAINS = {light, switch, fan, climate,
media_player}` — the real domains `homeassistant.turn_on/turn_off`
generically support — before a FILL will use it. A `lock`/`vacuum`/etc.
domain refuses rather than guessing (test E, engineered fixture — this
checkout's real registry only configures light/switch domains, both
compatible, so a genuine incompatible-domain example needs a
constructed fixture, same "no natural example, prove the gate anyway"
precedent as Sprint 52's own `test_T`).

**Investigated, not extended:** the brief's own examples ("Naikin
brightness" / "Set warna merah" with no device named) describe filling
brightness/color follow-ups from context too. Investigated directly:
`IntentParser.parse("naikin brightness")` and `IntentParser.parse("set
warna merah")` do NOT parse as `home_assistant` steps at all — they fall
to `"unknown"` (confirmed by direct probe, `test_E_naikin_brightness_
with_no_device_does_not_parse_as_a_fillable_ha_step`). Extending
context-fill to these action types would first require widening
`IntentParser`'s own grammar to recognize a target-less brightness/
color phrasing — a Planner-layer change this sprint's own hard
invariants caution against ("modify planner/LLM behavior unless
strictly required") and one this sprint found no live-reproduced need
for beyond the brief's own illustrative example. Documented here as a
proven, deliberate scope boundary, not an oversight.

## 9. Invalidation behavior

- **Freshness:** turn-age beyond `_CONTEXT_MAX_TURN_AGE` → refuse (§4).
- **Domain incompatibility:** → refuse (§8).
- **Failed/timed-out execution of the remembered target:** → un-
  remembered immediately (§5).
- **Same-turn multi-device ambiguity:** → memory cleared (§7).
- **`ConversationEnded`:** → both `_last_device_target` and `_device_
  context_turn_seq` popped for that conversation (test Q).
- **Device disappears from the live registry** between REMEMBER and
  FILL: NOT re-checked at the context layer itself (that would be a
  second resolver — exactly what the hard invariants forbid). Proven
  end-to-end instead: the FILL step still rewrites the text using the
  cached slug, but the rewritten command then reaches the unmodified
  Sprint 52 resolver, which correctly returns `UnknownDevice` with ZERO
  calls made to Home Assistant (test P) — the existing architecture's
  own safety net catches this without any redundant check.

## 10. Observability

One new structured Event Bus event, `device_context_resolution`,
published only when a contextual resolution is genuinely ATTEMPTED
(never for explicit commands) — same `self._event_bus.publish(Event(
type=..., data={...}))` pattern Sprint 50 established for `memory_
reference_classified` (guarded by `if self._event_bus is not None`,
wrapped in try/except so telemetry can never break a turn). Fields:
`conversation_id`, `attempted`, `resolved`, `candidate_count` (0 or 1 —
single-slot memory, not a ranked list), `target` (device slug, never
raw text), `refusal_reason` (`"no_memory"`/`"stale"`/`"incompatible_
domain"`/`None`), `turn_age`. This lets a dashboard/log inspection
distinguish explicit / Sprint-52-fuzzy / Sprint-56-differentiator /
contextual resolution / contextual refusal / contextual invalidation,
without adding a large new event taxonomy — one event, several bounded
fields, reused pattern. No raw utterance is ever logged, respecting the
existing redaction/field-size/retention conventions.

## 11. Tests

`tests/test_sprint57_ha_contextual_reference.py` (new, 19 tests) —
implements exactly the A–Q scenario matrix from this sprint's brief,
plus a performance test:

| scenario | test |
|---|---|
| A — basic contextual reference | `test_A_*` |
| B — explicit override | `test_B_*` |
| C — missing context | `test_C_*` |
| D — ambiguous context | `test_D_*` |
| E — unsupported capability | `test_E_*` (x2: domain-gate proof + IntentParser grammar-boundary proof) |
| F — context expiration | `test_F_*` |
| G — non-HA interruption | `test_G_*` |
| H — Sprint 52 fuzzy resolution | `test_H_*` |
| I — Sprint 56 query differentiator | `test_I_*` |
| J — exact entity | `test_J_*` |
| K — alias | `test_K_*` |
| L — ambiguous fuzzy match | `test_L_*` |
| M — repeated contextual commands | `test_M_*` |
| N — context switch | `test_N_*` |
| O — failed first command | `test_O_*` |
| P — device disappears | `test_P_*` |
| Q — session reset | `test_Q_*` |

**All 19 passed, 0 failed.** This file complements (does not replace)
the broader 42-test `tests/test_sprint57_contextual_ha_references.py`
suite already in this checkout, which covers the same mechanism from a
wider angle (safety-matrix labels A–V, message-quality, observability
event-shape tests, structural no-LLM checks) — both exercise the same,
single, unduplicated implementation.

## 12. Performance

`_apply_device_context()`: mean **~0.01–0.02ms/call** (500–1000
iteration measurements, both test files), maximum observed well under
1ms. Far under the 5ms target. No polling loop, no blocking network
call, no unnecessary HA API call — the entire contextual path is dict
lookups and one `luno.devices` registry scan; verified structurally (a
source-scan test confirms no `openai`/`openrouter.chat`/`requests.`/
`httpx.`/`embedding` reference in any new method).

## 13. Persistent-state verification

`find config -type f | xargs md5sum`, captured before this sprint's own
first change and re-diffed after every regression run (targeted suite,
full repository sweep): **byte-identical every time, zero drift**,
across every `config/*.json` file and the `vision_memory.sqlite3`/
`-shm`/`-wal` files. No contextual reference, successful or refused,
modifies any persistent configuration — all new state
(`_last_device_target`, `_device_context_turn_seq`) is in-process,
per-`PlannerBridgeModule`-instance, never persisted to disk.

## 14. Known limitations

- Cross-turn ambiguity between two DIFFERENT remembered devices cannot
  arise by construction (single-slot memory) — a deliberate safety/
  simplicity trade-off, not a gap (see §7).
- Brightness/color follow-ups with no device named do not parse as
  home_assistant steps at all in this checkout's current `IntentParser`
  grammar, so contextual fill cannot extend to them without a Planner-
  layer grammar change — investigated and proven, not implemented, per
  this sprint's own hard invariants (see §8).
- Domain-compatibility coverage for `fan`/`climate`/`media_player` is
  proven via engineered fixtures only — this checkout's real device
  registry has no natural example of either domain.
- "Device disappears" safety relies on the downstream Sprint 52
  resolver catching an unresolvable slug, not a live re-check at the
  context layer itself — correct and sufficient (proven end-to-end,
  zero HA calls made), but means a disappeared-device FILL is one hop
  slower to refuse than a stale/incompatible one, which refuses at the
  context layer directly.

## 15. Recommended next sprint

Multi-device group commands ("nyalain semua lampu" / "turn off
everything in the bedroom") are the next highest-value HA-adjacent gap
— unimplemented, outside every prior sprint's scope (including this
one). If a future sprint wants to extend contextual fill to brightness/
color follow-ups (§14), it should start by widening `IntentParser`'s
own grammar for a target-less `set_brightness`/`set_color` phrasing
(with its own dedicated safety/test pass), not by touching this
sprint's `_apply_device_context()` mechanism directly.
