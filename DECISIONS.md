# Luno Engineering Decisions

Every entry below is reconstructed from source-code docstrings, in-repo documentation (`ARCHITECTURE_GUARD.md`, `docs/change_impact/*.md`), or direct verification performed during a repository audit — never invented. Where the reasoning wasn't stated anywhere in the repo, that is noted explicitly rather than guessed at.

## Decision: Keep the legacy procedural script instead of deleting it

### Context
Luno originally shipped as one large procedural script with a hand-rolled conversation loop and direct hardware calls. An event-driven architecture (`luno/core` + everything built on it) was built to replace it.

### Decision
The old script was moved to `luno/main.py` (and, per `main.py`'s own docstring, a copy was also intended to live at root `legacy_main.py` — that second file is currently absent from this checkout, a known gap, not a deletion). Neither is rewritten or deleted; `python main.py` now runs the new launcher instead.

### Reason
Preserve a known-working reference implementation and avoid a risky big-bang rewrite; the two implementations intentionally do not share code (the legacy script imports flat modules like `luno/ha_client.py`/`luno/devices.py`/`luno/persona.py` directly; the event-driven world uses `luno/core`, `luno/adapters`, etc.).

### Alternatives Considered
Not stated in the repo beyond "keep as reference."

### Consequences
Two parallel implementations must be mentally tracked. `luno/main.py` (exists) and root `legacy_main.py` (absent) are easy to conflate — see `CURRENT_STATE.md`'s Known Limitations.

---

## Decision: Everything communicates through one Event Bus — no direct module-to-module calls

### Context
A system this size (behavior tree, planner, tool manager, adapters, memory, vision) needs a way to add new capabilities without every module needing to know about every other module.

### Decision
No module calls another module's methods directly. All communication is `EventBus.publish()`/`subscribe()`, and which module receives which event type is decided entirely by `Coordinator.add_route(event_pattern, module_name)` — a routing table, not `if event.type == ...:` chains inside module code.

### Reason
Documented directly in `luno/core/event_bus.py`'s own docstring ("Nothing in this codebase should ever call another subsystem's method directly") and reinforced by `luno/adapters/manager.py`'s docstring ("Adapters must never manually instantiate each other").

### Alternatives Considered
Not stated.

### Consequences
New functionality is added by registering a module + adding routes, not by editing existing modules to know about a new consumer. The flip side: an event type with no registered route silently reaches nobody — this is exactly what happened with `conversation_ended` never being routed to `"planner"` (see `CURRENT_STATE.md`).

---

## Decision: A failing Event Bus subscriber is throttled, never silently, permanently unsubscribed

### Context
Early in the project, a subscriber that started raising exceptions on every event would either crash the pump loop or (if caught generically) go silently deaf forever with no way to recover and no visible trace.

### Decision
A subscriber that fails 5 consecutive times is marked `degraded` and throttled with exponential backoff (1s initial, doubling, capped at 60s) — deliveries are skipped during the backoff window, and the next matching event after the window becomes the retry attempt. One success resets it to healthy. `subscriber_degraded`/`subscriber_recovered` events are published on each transition. The subscriber is never automatically removed.

### Reason
Directly stated in `event_bus.py`'s own docstring: distinguishes a *transient* problem (API timeout, HA briefly offline) from a permanent one, and makes the degraded state observable (Health Monitor, dashboard) instead of a silent failure.

### Alternatives Considered
The docstring explicitly contrasts this with the previous behavior ("used to make a module go silently deaf with no way back") — implying unsubscribe-on-failure or no protection at all were tried/considered and rejected.

### Consequences
A transient failure self-heals without a process restart. A permanently-broken handler costs at most one skipped delivery per backoff window (up to 60s) rather than spamming logs or CPU forever.

---

## Decision: Every adapter has a mock, and real backends are opt-in via environment variables

### Context
The system needs to be developable and testable without API keys, a real Home Assistant instance, a microphone, or a camera.

### Decision
Every adapter (`OpenRouterAdapter`/`LLMManagerAdapter`, `FishAudioAdapter`, `WhisperAdapter`, `HomeAssistantAdapter`, `VisionAdapter`, `UnityAdapter`) defines an abstract client interface plus a mock implementation with zero external dependencies; real implementations are separate files (`*_real.py`) enabled only via explicit config (`HOME_ASSISTANT_BACKEND=real`, `TTS_ENGINE`, `FISH_AUDIO_BACKEND=real`, etc.), defaulting to mock.

### Reason
Stated repeatedly across adapter docstrings (e.g. `fish_audio_real.py`: "Opt-in only... default stays Mock..., zero behavior change unless explicitly enabled").

### Alternatives Considered
Not stated.

### Consequences
The entire pipeline (including `main_runtime_demo.py`) runs end-to-end with no external services. Adding a new real backend never risks breaking the mock-based test suite.

---

## Decision: `LLMManagerAdapter` keeps the module id `"openrouter"` when replacing `OpenRouterAdapter`

### Context
A Multi-LLM Provider System sprint introduced `LLMManagerAdapter` to replace `OpenRouterAdapter` as what `bootstrap/adapters.py` actually constructs, with a byte-identical event contract.

### Decision
The new class registers under the same module id string, `"openrouter"`, rather than a new id like `"llm_manager"`.

### Reason
Stated directly in `llm_manager.py`'s docstring: that id is referenced by string in a dozen-plus places (other modules' `dependencies=[...]` lists, `bootstrap/health.py` checks, the dashboard's adapter table) — renaming would be a purely cosmetic change with real risk (every reference needs to move in lockstep) for zero behavioral benefit.

### Alternatives Considered
Renaming to reflect the new class name — explicitly rejected.

### Consequences
`OpenRouterAdapter` (the older class) still exists, still has its own tests, and is still directly usable — both classes coexist. Anyone searching for "which adapter handles the LLM" by module id alone will find `"openrouter"` even though `LLMManagerAdapter` may be what's actually running.

---

## Decision: No separate wake-word-spotting model at the Whisper Source layer

### Context
The legacy script used a dedicated `openwakeword` model that only spots the wake word, then swaps to a different Whisper-based command-listening mode once triggered.

### Decision
`RealWhisperSource` transcribes everything continuously; `luno.wake_session.SessionManagerModule` is the single place that decides whether an utterance should wake Luno, extend a session, or be dropped.

### Reason
Stated in `real_whisper.py`'s docstring: running a second, separate wake-word model at the Whisper layer would duplicate that decision in two places using two different mechanisms, risking disagreement. This also gets barge-in "for free" — the mic never stops listening, so `BargeInModule` can independently fan out off the same raw stream.

### Alternatives Considered
Reusing the legacy script's separate wake-word-spotting model — explicitly rejected as duplicating a decision the new architecture already centralizes elsewhere.

### Consequences
Every word spoken is transcribed (not just post-wake-word), which is what makes full-duplex barge-in possible without extra plumbing.

---

## Decision: `luno.wake_session` and `luno.barge_in` deliberately do not import each other

### Context
Both packages need to react to the same raw `speech_recognized` events for related but distinct purposes (session/wake-word state vs. interrupt classification).

### Decision
Neither package imports the other. Both independently subscribe to the same event, using ordinary Event Bus fan-out (the same mechanism `"motion"` already uses to reach both `behavior_tree` and `vision_memory`).

### Reason
Stated in both packages' `__init__.py` docstrings: a small amount of intentional duplication is preferred over cross-package coupling, keeping each independently testable with zero cross-package imports — matching the project's general architecture rule.

### Alternatives Considered
A shared base/coupling between the two — explicitly rejected.

### Consequences
Some logic (e.g. text matching conventions) may exist in slightly duplicated form between the two packages. This is accepted as the cost of independence.

---

## Decision: TTS playback publishes "started" only when audio actually begins, not when synthesis begins

### Context
`RealFishAudioClient` (GPT-SoVITS/F5-TTS) has two phases: HTTP synthesis (can take seconds) and actual audio playback. `SpeechPlaybackStarted` was originally published before `client.play()` was even called — harmless for the mock (no separate synthesis phase) but wrong for the real backend.

### Decision
`FishAudioClient.play()` takes an `on_playback_start` callback that the **client itself** invokes, and only at the true start of phase 2 (playback), never at the start of phase 1 (synthesis).

### Reason
A real, documented bug: Wake Session and Barge-In believed Luno was still idle/sleeping while a multi-second TTS synthesis request was in flight (because they only ever saw `SpeechPlaybackStarted` fire too early or not track the distinction at all), so barge-in never activated during that window.

### Alternatives Considered
Not stated beyond the fix itself.

### Consequences
Playback-lifecycle events are now trustworthy for downstream state machines (Wake Session, Barge-In) that gate behavior on "is Luno actually speaking right now."

---

## Decision: Dashboard uses stdlib `http.server`, not a web framework

### Context
The Dashboard needed a lightweight, read-only HTTP + streaming API.

### Decision
`http.server.ThreadingHTTPServer` + stdlib `json`/`urllib.parse`, with Server-Sent-Events (not WebSocket) for the two live-streaming views (Event Bus, Logs).

### Reason
Stated in `dashboard/server.py`'s docstring: the project has deliberately added zero new hard HTTP dependencies for any prior sprint (no Flask/FastAPI/aiohttp/uvicorn anywhere); SSE needs nothing beyond a long-lived plain HTTP response, avoiding a second asyncio event loop (unlike `real_home_assistant.py`, which genuinely needs one because the library it wraps is asyncio-only) and staying consistent with "every other package in this codebase is thread-based, not asyncio-based."

### Alternatives Considered
A real web framework, or WebSocket for live views — both explicitly rejected for adding dependencies/complexity not required by the actual need.

### Consequences
Dashboard code is slightly more verbose than it would be with a framework, but the project's dependency footprint stays minimal and consistent with the rest of the codebase's threading model.

---

## Decision: Extract a generic persistence helper, but do not migrate `long_term_memory.json` onto it

### Context
`long_term_memory.json` already had a hand-hardened atomic-write/backup/recovery implementation (`luno/memory.py`'s private `_atomic_write_json`/`_backup_current_memory_file`/etc.), built in direct response to a real data-loss incident. Six other JSON stores had no such protection.

### Decision
`luno/persistence.py` was created as a new, generic, domain-agnostic module implementing the same contract (atomic write, pre-write backup, retention, corruption recovery, pytest write guard), and the six other stores were wired to it. `long_term_memory.json`'s own original implementation in `memory.py` was left completely untouched — not rewritten to call the new shared module.

### Reason
Stated in `persistence.py`'s own module docstring: `memory.py`'s six functions "are NOT replaced or rewritten by this module - they remain the reference implementation, documented as a CONTRACT." Not migrating the most sensitive store (the one that had already suffered real data loss) avoided any risk of the generalization introducing a regression in the one store that already had proven, battle-tested protection.

### Alternatives Considered
Migrating all seven stores (including `long_term_memory.json`) onto the shared helper for full consistency — implicitly rejected in favor of "reference implementation stays as-is, everything else follows it."

### Consequences
There are now two slightly different, independently-maintained atomic-write implementations doing conceptually the same thing. `long_term_memory.json`'s backup-before-write is best-effort; `luno/persistence.py`'s is mandatory (see next decision) — a deliberate, documented divergence, not an oversight.

---

## Decision: The new persistence helper makes backup-before-write mandatory, stricter than the original

### Context
`memory.py`'s original `_save()` treats a backup failure as non-fatal (logs and continues to write anyway).

### Decision
`luno/persistence.py`'s `atomic_write_json()` raises `BackupFailedError` and refuses to write at all if a pre-write backup of an existing primary file fails.

### Reason
Documented directly in `persistence.py`: "backup failure HARUS mencegah destructive overwrite" (backup failure MUST prevent a destructive overwrite) — a deliberately stricter policy than the original, explicitly called out as an intentional difference (see `docs/change_impact/persistent_state_hardening_v2.md` §4).

### Alternatives Considered
Matching `memory.py`'s best-effort behavior exactly, for consistency — rejected in favor of the stricter guarantee for the six newly-hardened stores.

### Consequences
A write to any of the six non-reference stores can now fail loudly (raise) in a scenario where the original `long_term_memory.json` path would have quietly proceeded. Any future caller must handle `BackupFailedError`, not assume every write silently succeeds.

---

## Decision: Response depth is computed by deterministic rules, never a second LLM/API call

### Context
The Response Depth Policy sprint needed to classify each turn as SHORT/NORMAL/DETAILED.

### Decision
`luno/response_policy.py` uses bounded phrase-matching and keyword buckets over plain text — no LLM call, no external API, no second AI model, no persistence.

### Reason
An explicit, hard requirement of the sprint brief, and enforced structurally: the module imports nothing beyond the standard library (verified — no network-capable import anywhere in the file), and this is checked by a dedicated source-scan test, not just assumed.

### Alternatives Considered
Using an LLM call to classify desired depth — explicitly disallowed by the brief.

### Consequences
The policy is fast (sub-millisecond), fully deterministic, and testable without mocking a network call — but it is only as good as its hand-curated phrase/keyword lists, and will not generalize to phrasing outside them without an explicit code change.

---

## Decision: Response-depth internal scoring stays internal; only three depth values are ever exposed publicly

### Context
The design could have exposed a 5-level internal scale (MINIMAL/SHORT/NORMAL/DETAILED/EXHAUSTIVE) to the rest of the application.

### Decision
`ResponsePolicy.depth` resolves to exactly one of `"short"`/`"normal"`/`"detailed"`. The internal 0-100 `score` and `reasons` exist for explainability/debugging but are never surfaced to the end user, and no 5-level enum was implemented at all — the sprint brief's own "do not overengineer" instruction was taken literally.

### Reason
Explicit instruction in the sprint brief ("do not expose unnecessary complexity to the rest of the application"), and a deliberate implementation choice to skip the optional internal 5-level scale entirely since three buckets over one bounded score already satisfied every required behavior.

### Alternatives Considered
Building the full 0-4 internal enum "for future flexibility" — considered and rejected as unnecessary for what the sprint actually required.

### Consequences
If a future sprint genuinely needs finer-grained internal levels, that is new work, not a matter of un-hiding something that already exists.

---

## Decision: `RoutingDecision.complexity` and `ResponsePolicy.score` are kept as two separate, unmerged concepts

### Context
`luno/routing/decision_engine.py` already computes a `ComplexityLevel` (low/medium/high/extreme) per turn, for choosing which LLM provider to route to. The Response Depth Policy sprint needed a similar-sounding "how complex is this question" signal.

### Decision
`response_policy.py` implements its own independent task-type/complexity classification rather than reusing `RoutingDecision.complexity`.

### Reason
Verified directly during that sprint's own architecture audit: `RoutingDecision` is computed very late in `_handle_utterance()` (near the end, right before `NeedLLMResponse` is published), while the response-depth policy needs to be computed early (alongside memory retrieval/emotion estimation) — reusing it would create an ordering dependency. The two also represent genuinely different axes: provider-routing complexity vs. reply-length complexity. Conflating them was judged a conceptual error, not just an implementation inconvenience.

### Alternatives Considered
Reusing `RoutingDecision.complexity` directly to avoid "duplicate policy calculation" — considered and rejected for the reasons above.

### Consequences
Two independent classifiers exist that both use the word "complexity" in different senses. A future reader must not assume they're the same signal.

---

## Decision: `luno/memory_context.py` is a selection layer, not a new memory system

### Context
Two previously-independent Manual Memory prompt-assembly paths overlapped.

### Decision
`memory_context.py` adds exactly one read-only, bounded, deterministic selection step deciding which of the *existing* memory/context providers are relevant for the current turn — it is explicitly not a new store, retriever, tokenizer, importance scale, lifecycle system, or conflict resolver, and never mutates persistent memory (no reinforcement, archiving, deletion, or new writes of any kind).

### Reason
Stated directly in the module's own docstring, with an explicit list of what it deliberately is not, to prevent scope creep in future work on it.

### Alternatives Considered
Building a new unified memory store — explicitly rejected; the existing stores (`luno.memory`, `luno.memory_retrieval`, `luno.episodic_memory`, `luno.memory_guard`, `luno.relationship_engine`) remain untouched and are the only writers.

### Consequences
Any future change to this module that adds a write path would violate its own stated contract and should be treated as a red flag, not a natural evolution.
