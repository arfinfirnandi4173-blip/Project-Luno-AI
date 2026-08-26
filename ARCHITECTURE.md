# Luno Architecture

This document describes the architecture actually present in this repository as of this handover. It documents what exists, not a target design. For the full contract-level detail (protected interfaces, exact file/line references, known risks per subsystem), see `ARCHITECTURE_GUARD.md` — this file is the readable overview; that one is the enforceable detail.

## Overview

Luno is a desktop voice-assistant / VTuber companion: conversational AI, Home Assistant smart-home control, camera vision, persistent long-term memory, reminders, app launching, web search, and a lip-synced 3D avatar (VNyan or web VRM) driven by voice-cloned TTS.

The repository contains **two implementations side by side**:

1. **Event-driven architecture** (`luno/core` + everything built on it) — the current, actively developed system. Nothing calls another module directly; all communication is `publish()`/`subscribe()` through one Event Bus, routed by a `Coordinator` routing table.
2. **Legacy procedural script** (`luno/main.py`, ~1866 lines) — the original, pre-event-driven implementation (direct hardware calls, one big conversation loop, imports flat modules like `luno/ha_client.py`, `luno/devices.py`, `luno/persona.py` directly). Preserved unchanged as a reference; not what `python main.py` runs anymore. A second file, root-level `legacy_main.py`, is referenced by `main.py`'s own docstring and by `tests/test_root_main_bargein.py` but is **absent from this checkout** — a known, pre-existing gap (see Known Limitations in `CURRENT_STATE.md`). `luno/main.py` and root `legacy_main.py` are two separate files; do not assume they are the same thing.

`main.py` (repo root) is the official production entry point — a thin launcher assembling `luno/bootstrap/*` (real adapters, module registration, health checks, dashboard, no business logic itself). `main_runtime_demo.py` (~260KB) is simultaneously the developer console (mocks every piece of hardware by default) **and** the only place four core "Module Bridge" classes are implemented — `PlannerBridgeModule`, `ToolManagerBridgeModule`, `BehaviorTreeModule`, `VisionMemoryModule` — which `luno/bootstrap/modules.py` imports and uses for production too. Modifying `main_runtime_demo.py` affects both the dev console and production.

## System Components

### `luno/core/` — the event-driven substrate
- `event_bus.py` — `EventBus`: non-blocking `publish()` (queue-based), one background pump thread dispatches to subscribers by descending priority. Supports sync and async (dispatcher-submitted) subscribers, `fnmatch`-style wildcard patterns (`"*"`, `"tool_*"`). **Self-healing degraded-subscriber handling**: a subscriber that raises 5 times consecutively is marked `degraded` and throttled with exponential backoff (1s → doubling, capped at 60s) rather than unsubscribed — it recovers automatically on its next success. `subscriber_degraded`/`subscriber_recovered` events are published on each transition.
- `dispatcher.py` — thread pool that actually executes async-mode event handlers.
- `coordinator.py` — the routing table: `add_route(event_pattern, module_name)`. This is the **only** mechanism by which a published event reaches a given module's `on_event()`. A module's `on_event()` handling an event type it was never routed does nothing — this is a real, discoverable trap (see Known Limitations).
- `module_manager.py` — module registration + dependency-ordered start/stop; one module failing to start does not fail the whole `Runtime.start()`.
- `lifecycle.py` — per-module start/stop/restart with fault isolation.
- `scheduler.py` / `heartbeat.py` — periodic tick loop + system heartbeat.
- `health.py` — aggregates health status across all modules.
- `events.py` — ~24+ core `Event` types (`SystemStarted`, `SpeechRecognized`, `HomeAssistantEvent`, etc.); `luno/adapters/events.py` adds ~29 more adapter-layer event types.
- `context_builder.py` — builds the LLM conversation context (distinct from `memory_context.py`, see Memory Architecture).
- `runtime.py` — `Runtime`: the single assembly point for `EventBus`, `Dispatcher`, `Scheduler`, `HeartbeatMonitor`, `ModuleManager`, `Coordinator`, `LifecycleManager`, `HealthMonitor`. Public API: `start()`, `stop()`, `restart()`, `reload()`, `health()`, `status()`.

### `luno/adapters/` — the only layer allowed to talk to the outside world
Pure translation between internal Events and external APIs/hardware — no AI/planning logic lives here. Every adapter has a mock implementation so the whole system runs without any API key or hardware.

- `openrouter.py` (`OpenRouterAdapter`) / `llm_manager.py` (`LLMManagerAdapter`) — LLM integration. **`LLMManagerAdapter` is what production actually registers** (Multi-LLM Provider System sprint), deliberately keeping the module id `"openrouter"` for backward compatibility with every existing `dependencies=[...]` reference. Event contract is byte-identical to the older `OpenRouterAdapter`, which still exists, still has its own tests, and is still usable directly — both classes coexist on purpose.
- `whisper.py` / `real_whisper.py` — STT. Real implementation reuses `legacy_main.py`'s `get_whisper_model()`/`transcribe_audio()` (Faster-Whisper, local/offline). Deliberately has **no separate wake-word-spotting model** — it transcribes everything continuously; `luno.wake_session.SessionManagerModule` is the single place wake-word decisions are made, so barge-in works "for free" (mic never stops listening).
- `fish_audio.py` / `fish_audio_real.py` — TTS playback. `RealFishAudioClient` talks to GPT-SoVITS/F5-TTS servers. Two-phase design (synthesis, then playback) — `SpeechPlaybackStarted` fires only when phase 2 (actual audio) begins, not when synthesis starts (a real bug fix: previously Wake Session/Barge-In believed Luno wasn't speaking while a multi-second TTS request was in flight). Playback runs on its own dedicated executor so control events (pause/stop) aren't queued behind a long `play()` call.
- `home_assistant.py` / `real_home_assistant.py` — HA integration, wraps the existing `luno.ha_client.HomeAssistantClient` websocket client (not reimplemented). Inbound: real HA state changes → `DeviceStateChanged`/`HomeAssistantEvent`. Outbound: `tool_requested` events targeting `"home_assistant"` forwarded to `call_service()` — a convenience path, **not** a replacement for `luno.tool_manager.builtin.home_assistant`'s handler, which remains the canonical execution path for Planner-issued tool calls.
- `vision.py` / `real_vision.py`, `unity.py` / `real_unity.py`, `scheduler.py` — camera vision, avatar/Unity bridge, scheduled jobs.
- `manager.py` (`AdapterManager`) — thin facade over `ModuleManager`/`Coordinator`/`EventBus` adding adapter-specific enable/disable config and event-route auto-wiring from a configurable `EventMapping` (`models.py`) — explicitly NOT hardcoded `if event.type == ...` routing.

### `luno/behavior_tree/` — "what should happen right now"
Priority state machine: Emergency → Critical HA event → direct user speech (Listening) → tool execution → conversation continuation → passive Watching → Proactive → Idle. Each action dispatches async on its own executor, never blocking the tick loop.

### `luno/planner/` — "how to execute a request"
Turns a user request into one or more dependency-ordered `ToolCall`s with retry/rollback/cancel/pause-resume. Never talks to hardware directly — only produces generic `ToolCall`s like `{"tool": "home_assistant", "action": "turn_on", "target": "bedroom_light"}`.

### `luno/tool_manager/` — universal execution layer
Receives a generic `ToolCall` (from the Planner, but doesn't import it — only depends on the data shape), finds the right handler via `registry.py`, executes with timeout/retry/cancel, always returns a structured `ToolResult`. Never generates language, never calls an LLM. `builtin/` holds concrete handlers (home_assistant, browser, camera_ptz, windows, spotify, vision, unity, llm_mode) each with real + mock variants.

### `luno/routing/` — Intelligent AI Routing (which LLM/provider handles this turn)
`DecisionEngine.decide()` — pure orchestration over `intent_classifier.py`, `complexity_estimator.py` (a `ComplexityLevel` enum: low/medium/high/extreme — a **different concept** from `luno/response_policy.py`'s 0-100 response-length score; deliberately not reused for that purpose), `knowledge_router.py`, `provider_selector.py`, `affinity.py` (sticky reasoning-provider per conversation), `web_search_router.py`. Called once per turn near the end of `PlannerBridgeModule._handle_utterance()`, right before publishing `NeedLLMResponse`. Never bypasses Planner/Tool Manager, never fabricates a result.

### `luno/wake_session/` — wake word + conversation session state
`SLEEPING → AWAKENING → LISTENING/THINKING/SPEAKING/WAITING_USER → (timeout) → SLEEPING`. The wake word only needs to be spoken while `SLEEPING`; once a session is open, all speech is processed directly until `session_timeout_s` of inactivity elapses. Standalone package, does not import `core`/`adapters`/`behavior_tree`/`planner`/`tool_manager`/`vision_memory`.

### `luno/barge_in/` — interrupting Luno mid-speech/thought
Four modes: **FREE** (stop immediately + short acknowledgment), **SOFT** (voice stops, task keeps running in background), **CONFIRM** (asks yes/no before cancelling), **CRITICAL** (only pauses, never silently cancels, used during active emergencies). Deliberately does not import `luno.wake_session` (or vice versa) — both independently subscribe to the same raw `speech_recognized` events, same fan-out mechanism `"motion"` uses to reach both `behavior_tree` and `vision_memory`.

### `luno/vision_memory/` — persistent visual awareness
Sits between the vision model (Gemini 2.0 Flash per current `vision.py`, MiniCPM-V via Ollama historically) and the LLM: converts a stream of raw per-frame descriptions into an always-current world state, a deduplicated log of only *meaningful* changes (scored 1-5, threshold 3), and slowly-learned habits. The LLM only ever sees `get_world_state()`/`get_recent_events()` summaries, never raw frame-by-frame data. Backed by SQLite (`config/vision_memory.sqlite3`).

### `luno/world_model.py` — "what is the world like right now"
Single source of truth for current device state (e.g. `light.bedroom = on`), built entirely on top of existing pieces — HA's `device_state_changed` events, `RealHomeAssistantSource.get_all_states()` for startup sync, and `ToolResult.success`/`.data` for verified tool-execution truth. Deliberately **not** a database about the user (that's Memory) — only current device state, no polling anywhere.

### `luno/text_normalizer/` — TTS-friendly text
Converts numbers/abbreviations/symbols into speakable form, Indonesian (`numbers_id.py`) and English (`numbers_en.py`) variants.

### `luno/dashboard/` — read-only operational visibility
`server.py` (`DashboardServer`) — stdlib `http.server.ThreadingHTTPServer` only (deliberately zero new HTTP framework dependency), serving JSON endpoints + two Server-Sent-Events streams (Event Bus, Logs) + one static HTML page. Every read goes through `collectors.py`, which only ever reads `runtime.*`/`adapter_manager.status_all()`/module public methods — never calls an adapter directly, never duplicates Runtime state.

## Runtime Flow

Full path for one conversational turn (developer console; production is the same, real adapters instead of mocks):

```
"Luno, buka chrome"
  -> SpeechRecognized                         (Whisper adapter, real or simulated)
  -> SessionManagerModule (wake-word gate; Sleeping -> Listening)
  -> "user_utterance"
  -> PlannerBridgeModule._handle_utterance()   (see below)
       - memory retrieval (once)
       - usage tracking
       - emotion estimation
       - Response Depth Policy (once)
       - Planner creates a plan / tasks
       - Tool Manager executes tasks -> ToolResult
       - Routing Decision Engine selects LLM provider
       - notes[] assembled: persona -> depth instruction -> relationship
         -> memory -> verified facts -> session summary -> emotional
         context -> language/character reminder
       - NeedLLMResponse published (system_prompt = "\n\n".join(notes))
  -> LLM Manager / OpenRouter Adapter -> LLMChunk*(streaming) -> LLMFinished -> AssistantResponse
  -> BehaviorTreeModule._speak() (build_dual_response(): normalize_for_speech()
     + depth-aware compression + TTS chunk segmentation, then SpeakRequest
     carrying both "text" (full voice_text) and "chunks" (ordered pieces))
  -> Fish Audio Adapter (plays "chunks" sequentially if present, else falls
     back to one "text" block - see luno/adapters/fish_audio.py)
  -> SpeechPlaybackFinished -> WaitingUser
  -> (no new speech within session_timeout_s) -> Sleeping
```

At any point after Luno starts speaking, an interrupt word routes independently through `BargeInModule`, which decides FREE/SOFT/CONFIRM/CRITICAL based on the reply's classified `SpeakingMode`.

## Event Flow

- **Publish never blocks.** `EventBus.publish()` is a non-blocking queue push; under sustained backpressure the queue can fill and an event is dropped (counted in `stats()['dropped']`) rather than the publisher blocking.
- **Routing is a table, not code.** `Coordinator.add_route(event_pattern, module_name)` is the only thing that makes an event reach a module's `on_event()`. `main_runtime_demo.py` registers all of production's routes in one place (~line 4010-4035). **Only `user_utterance` is routed to `"planner"`** — `conversation_ended` is NOT, which is why `PlannerBridgeModule._on_conversation_ended()` is unreachable via the real event bus in this console (see Known Limitations in `CURRENT_STATE.md`).
- **Degraded-subscriber self-healing** (see Core Components above) means a transient failure in one module's handler never permanently deafens it and never needs a process restart — it resumes automatically once its own error clears.
- Adapter-layer event routing (Event → external API direction) is separately configurable via `EventMapping`/`DEFAULT_ADAPTER_EVENT_MAPPING` (`luno/adapters/models.py`), not hardcoded either.

## Memory Architecture

Luno has **seven** persistent JSON-backed stores plus one SQLite store, each owned by exactly one module:

| Store | File | Owner module |
|---|---|---|
| Long-term memory | `config/long_term_memory.json` | `luno/memory.py` |
| Session summaries | `config/session_summaries.json` | `luno/memory.py` |
| Relationship state | `config/relationship_state.json` | `luno/relationship_engine.py` |
| Episodic memory | `config/episodic_memory.json` (normally absent — created lazily on first save, absence is expected, not an error) | `luno/episodic_memory.py` |
| Habit memory | `config/habit_memory.json` | `luno/proactive/habit_memory.py` |
| Reminders | `config/reminders.json` | `luno/reminders.py` |
| Verified facts | `config/verified_facts.json` | `luno/memory_guard.py` (`VerifiedFactStore`) |
| Vision world state / events / habits | `config/vision_memory.sqlite3` | `luno/vision_memory/database.py` |

Plus a **read-only, non-writable selection layer**: `luno/memory_context.py` — not a store, not a retriever, not a scorer. It is a deterministic, bounded, read-only step that decides, per turn, which of the *existing* memory/context pieces (Manual Memory, Episodic, Verified Facts, Relationship) are relevant enough to hand the LLM, unifying two previously-overlapping prompt-assembly paths. It never mutates anything — no reinforcement, no archiving, no deletion.

Memory *retrieval/ranking* (relevance scoring, adaptive behavior) lives in `luno/memory_retrieval/` and `MemoryRetriever`/`ContextItem._rank_key()` — the ranking tuple order is `(relevance, importance, context_evidence, usefulness, evaluation, usage_count, priority)`, and evaluation (a learned usefulness signal) participates directly in ranking, not just observation, per the Memory Decision Quality & Adaptive Retrieval sprint.

`luno/response_policy.py` is explicitly **not** part of this memory system — it decides response length (SHORT/NORMAL/DETAILED), imports nothing from any memory module, and persists nothing.

## Persistence Architecture

Every writable JSON store above (all seven) routes through one shared, generic, domain-agnostic helper: **`luno/persistence.py`**. It knows nothing about memory/relationship/habit semantics — it only operates on `(path, data)`.

Sequence for every write (`atomic_write_json()`):
1. Refuse if running under pytest against a non-`tmp_path` location (`refuse_if_pytest_targeting_unisolated_path` — defense in depth; the real isolation mechanism is `tests/conftest.py`'s autouse fixture).
2. If a primary file already exists, copy it into `config/backups/<store>.<YYYYMMDDTHHMMSSffffff>.json` **before** touching the primary. If this backup fails, the write is refused entirely (`BackupFailedError`) — never a destructive overwrite with no safety net.
3. Write to a temp file in the **same directory** as the target (same filesystem, so the final step is atomic).
4. `flush()` + `fsync()`.
5. `os.replace()` the temp file onto the primary — atomic on POSIX; a crash before this point leaves the original file completely untouched.
6. Prune old backups down to a retention count (default 20, **never fewer than 1** regardless of misconfiguration).

Reads (`safe_load_json()`): missing file → caller-supplied default (each store's own pre-existing default, e.g. `[]` or `{}` — this helper never invents a shape); malformed JSON or wrong shape → default, or (only for stores that explicitly opt in via `recover_from_backup=True`) the newest backup that parses and validates. Every store's specific missing/malformed/recovery behavior is preserved exactly as it was before this helper existed — the helper generalizes the *mechanism*, not the *policy*.

`config/long_term_memory.json` (`luno/memory.py`'s `_load()`/`_save()`) is the **original, independent reference implementation** this helper was extracted from — it was never migrated to call `luno/persistence.py`, and is documented as staying that way (see `DECISIONS.md`).

## Tool / Integration Architecture

`luno/tool_manager/builtin/` handlers are the canonical execution path for every Planner-issued action: Home Assistant, browser (Playwright, opt-in via `BROWSER_ENABLED`), camera PTZ (Tapo, opt-in via `CAMERA_PTZ_BACKEND=real`), Windows desktop control, Spotify, vision, Unity/avatar, LLM-mode meta-tool. Each has a mock and a real variant; real variants are opt-in via env vars and additive — nothing about the mock path changes when a real backend is wired in.

## Home Assistant Integration

Two independent directions, both pure translation, no business logic:
- **Inbound**: real HA websocket connection (wraps the existing `luno/ha_client.py`, not reimplemented) → `HomeAssistantAdapter` publishes `DeviceStateChanged`/`AutomationTriggered`/`HomeAssistantEvent`.
- **Outbound (Planner path)**: `luno.tool_manager.builtin.home_assistant`'s handler — the canonical path for actual smart-home actions, with verification (expected vs. actual state) feeding `World Model` and `Memory Guard`'s "verified facts only" contract.
- **Outbound (adapter convenience path)**: `HomeAssistantAdapter` also listens for `tool_requested` events targeting `"home_assistant"` and forwards to `call_service()` directly — a separate, lower-level convenience, not used by the main Planner-driven flow.

Opt-in real backend via `HOME_ASSISTANT_BACKEND=real`; default is always mock, so the whole system runs without any HA instance.

## Error Handling

- Every "note" contributor in `PlannerBridgeModule._handle_utterance()` (memory retrieval, emotion estimation, response policy, relationship context, etc.) is wrapped in its own `try/except` that logs and never raises — one subsystem's bug degrades that one note to absent/default, never breaks the turn.
- Module-level fault isolation: one module failing `start()` doesn't fail `Runtime.start()` (see `health()`/`status()`).
- Event Bus subscriber failures self-heal via the degraded/backoff mechanism described above — no permanent silent failure, no manual restart needed for a transient issue.
- Persistence writes never partially corrupt a store: atomic replace + pre-write backup means a crash mid-write leaves the previous valid state intact.

## Recovery

- **Persistence layer**: `luno/persistence.py`'s `load_latest_valid_backup()` — newest-first, JSON-valid, optionally shape-validated — used by stores that opt into `recover_from_backup=True`. `long_term_memory.json` has its own independent, older recovery implementation (`_load_latest_valid_backup()` in `memory.py`) predating the shared helper.
- **Historical incident**: a real production data-loss incident on `long_term_memory.json` was recovered from a `2026-07-23` zip snapshot; the full recovery process (audit, migration script, isolated validation, backup mechanism build, restore, post-restore verification) is preserved under `recovery/` (`recovery_decision.md`, `migrate_snapshot.py`, `restore_to_production.py`, `validate_candidate_isolated.py`, `recovery_manifest.json`) — this is what directly motivated building `luno/persistence.py` and hardening the other six stores afterward. Do not delete `recovery/` — it's the historical record of why the persistence hardening work exists.

## Testing Architecture

- `tests/` (root-level, pytest-based) — **1445 tests collected** as of this handover (2 additional files, `test_main_bargein.py` and `test_root_main_bargein.py`, fail to *collect* for environment reasons — see `CURRENT_STATE.md`). An autouse fixture in `tests/conftest.py` (`isolate_persistent_state`) redirects every writer-capable JSON store path + the Vision Memory SQLite DB to a fresh `tmp_path` before every single test in this directory — this is load-bearing safety infrastructure, not optional. Console-integration tests (`test_runtime_demo.py`, `test_barge_in_console.py`, `test_wake_session_console.py`, `test_wake_barge_in_integration.py`, `test_real_fish_audio_console.py`) load `main_runtime_demo.py` via `importlib.util.spec_from_file_location` rather than a normal import, since it isn't structured as an importable package module.
- `luno/*/tests/` (package-level, e.g. `luno/core/tests`, `luno/adapters/tests`, `luno/wake_session/tests`, `luno/barge_in/tests`, `luno/routing/tests`, `luno/tool_manager/tests`, `luno/planner/tests`, `luno/browser/tests`, `luno/bootstrap/tests`, `luno/text_normalizer/tests`) — 37 files, each package independently testable with its own `SCENARIOS` list + `@scenario` decorator + `[PASS]/[FAIL]` runner convention, runnable via `python3 -m luno.<pkg>.tests.test_<pkg>` or directly.
- Run commands: `python3 -m pytest tests/ -q` (full root suite; expect ~9 known environment-specific failures, see `CURRENT_STATE.md`), `python3 -m pytest tests/test_<name>.py -q` (single file), `python3 main_runtime_demo.py` (interactive dev console).

## Important Interfaces

- `Event(type: str, source: str = ..., data: dict = ...)` — the universal message shape (`luno/core/events.py`).
- `EventBus.publish(event)` / `subscribe(pattern, handler, priority=0, async_mode=False, once=False)`.
- `Coordinator.add_route(event_pattern, module_name)` — the only way an event reaches a module.
- `ToolResult.ok(...)` / `ToolResult.fail(...)` — the only shape a Tool Manager handler ever returns; carries `expected_state`/`actual_state`/`verification_attempts` for verified actions.
- `luno.persistence.atomic_write_json(path, data, *, backup=True, retention=20)` / `safe_load_json(path, default, *, validate=None, recover_from_backup=False, log_prefix=None)` — the shared persistence contract every JSON store (except `long_term_memory.json`) uses.
- `luno.response_policy.compute_response_policy(text, *, previous_score=None) -> ResponsePolicy(depth, score, reasons, explicit, task_type)` / `build_depth_instruction(policy) -> str`.
- `PlannerBridgeModule._handle_utterance(event)` (`main_runtime_demo.py`) — the one place per-turn context assembly happens; new "notes" contributors are added here, not anywhere else.
- `NeedLLMResponse{request_id, system_prompt, ...}` — the contract between Planner/Behavior Tree and the LLM adapter layer; `system_prompt` is built as `"\n\n".join(notes)`.
