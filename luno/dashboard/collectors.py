"""
collectors.py
==============

Pure, read-only "Runtime state -> JSON-safe `dict`" functions - one per
Dashboard view. Every function here reads the EXACT SAME already-public
accessor `luno/bootstrap/console.py`'s own `print_*` methods already
use (`runtime.health()`, `session_manager.status_snapshot()`,
`planner_module.planner.get_plan()`, `vm.get_recent_events()`, ...) -
this module is not a second, independently-derived view of Runtime
state; it is the SAME view, serialized as JSON instead of formatted for
a terminal. No function here ever calls an adapter's own client
directly (no `fish_adapter.client.play()`-style calls anywhere in this
file) and no function ever mutates anything - see `controls.py` for the
(small, clearly-separated) set of functions that do.

Every dict returned here is guaranteed JSON-serializable (datetimes are
`.isoformat()`, enums are `.value`, dataclasses go through their own
`.to_dict()` where one exists) - `server.py` never needs a custom JSON
encoder.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from luno.bootstrap.launcher_config import LauncherConfig

#: Env var name fragments that must NEVER appear in a value shown by the
#: dashboard - see `collect_configuration()`. Matched case-insensitively
#: against the KEY, not the value (a value could coincidentally look
#: like a key some other secret uses; the safe rule is "if the setting's
#: NAME suggests a secret, hide the value unconditionally").
_SECRET_KEY_FRAGMENTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(frag in upper for frag in _SECRET_KEY_FRAGMENTS)


# ============================================================================
# Home page / Runtime Status
# ============================================================================

def collect_status(runtime: "Runtime", adapter_manager: "AdapterManager", launcher_config: "LauncherConfig") -> Dict[str, Any]:
    """Home page "Runtime Status" card - version/build/uptime/current
    time/state/wake word/model/tts/memory mode/interrupt/planner/tool
    manager - reuses `bootstrap.banner.build_runtime_status()` (the SAME
    function `/status` and the startup banner already call) and adds the
    handful of fields that are dashboard-specific (uptime, current wall
    clock, a single derived "Runtime State" word)."""
    from luno.bootstrap.banner import build_runtime_status

    base = build_runtime_status(runtime, adapter_manager, launcher_config)
    runtime_status = runtime.status()

    conversation = collect_conversation(runtime, {})
    tool_manager_enabled = "tool_manager" in base["modules_loaded"]

    return {
        **base,
        "uptime_s": runtime_status["uptime_s"],
        "running": runtime_status["running"],
        "current_time": datetime.now(timezone.utc).isoformat(),
        "runtime_state": conversation.get("state", "unknown"),
        "tool_manager_enabled": tool_manager_enabled,
        "python_version": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
    }


def collect_ping(runtime: "Runtime") -> Dict[str, Any]:
    """The single cheapest possible endpoint - what the frontend polls
    to decide "Runtime Offline" vs online, and to drive auto-reconnect.
    Deliberately does almost no work (no adapter/module iteration) so it
    stays fast even under load / while Runtime itself is degraded."""
    try:
        status = runtime.status()
        return {"ok": True, "running": bool(status.get("running")), "healthy": bool(status.get("healthy"))}
    except Exception as ex:
        return {"ok": False, "running": False, "healthy": False, "error": str(ex)}


# ============================================================================
# Modules
# ============================================================================

def collect_modules(runtime: "Runtime") -> List[Dict[str, Any]]:
    """Live Module Status - every registered module, its state
    (Running/Stopped/Restarting/Error/Disabled), restart count, last
    error, and a best-effort "last heartbeat" (module-level `health()`
    is called live here - the same call `ModuleManager.health_of()`
    already makes for `/health` - cheap and always safe by contract,
    see `Module.health()`'s own docstring)."""
    out: List[Dict[str, Any]] = []
    for name, record in sorted(runtime.module_manager.all_modules().items()):
        try:
            health = runtime.module_manager.health_of(name)
            healthy, health_message = health.healthy, health.message
        except Exception as ex:
            healthy, health_message = False, f"health() raised: {ex}"
        out.append({
            "name": name,
            "state": record.state.value,
            "dependencies": list(record.dependencies),
            "lazy": record.lazy,
            "restart_count": record.restart_count,
            "last_error": record.error,
            "started_at": _iso(record.started_at),
            "healthy": healthy,
            "health_message": health_message,
        })
    return out


# ============================================================================
# Adapters
# ============================================================================

def collect_adapters(adapter_manager: "AdapterManager") -> List[Dict[str, Any]]:
    """Adapter Status - Whisper/Vision/OpenRouter/Fish Audio/Home
    Assistant/Unity/Scheduler. Reuses `AdapterManager.status_all()`
    verbatim (events_in/events_out/consecutive_failures/restart_count/
    uptime_s/last_error/enabled/module_state - see `adapters/base.py`'s
    `BaseAdapter.status()`). "Average latency" is honestly omitted per
    adapter where the adapter itself doesn't track per-call timing
    (most don't today) rather than fabricating a number - see the
    `avg_latency_ms` field, present only for adapters whose
    `_extra_status()` already reports one."""
    out: List[Dict[str, Any]] = []
    for name, entry in sorted(adapter_manager.status_all().items()):
        connected = bool(entry.get("consecutive_failures", 0) == 0) and entry.get("module_state") == "running"
        out.append({
            "name": name,
            "enabled": entry.get("enabled", False),
            "module_state": entry.get("module_state", "unknown"),
            "connected": connected,
            "events_in": entry.get("events_in", 0),
            "events_out": entry.get("events_out", 0),
            "consecutive_failures": entry.get("consecutive_failures", 0),
            "restart_count": entry.get("restart_count", 0),
            "uptime_s": entry.get("uptime_s", 0.0),
            "last_error": entry.get("last_error"),
            "last_event_at": entry.get("last_event_at"),
            "avg_latency_ms": entry.get("avg_latency_ms"),
        })
    return out


# ============================================================================
# LLM Manager (Multi-LLM Provider System sprint)
# ============================================================================

def collect_llm_status(adapter_manager: "AdapterManager") -> Dict[str, Any]:
    """LLM panel - spec's own field list: Current Provider, Current
    Model, Streaming, Latency, Average Response Time, Requests, Tokens,
    Estimated Cost, Fallback Status, Health, Provider Capabilities,
    Provider Health (per provider). Reads `LLMManagerAdapter.status()`
    (registered under the module id `"openrouter"` - see
    `luno/adapters/llm_manager.py`'s own docstring for why) exactly the
    same way `collect_adapters()` reads every other adapter - no
    provider-specific branching lives in this function; it only
    reshapes what the adapter already reports."""
    registry = getattr(adapter_manager, "registry", None)
    adapter = registry.get("openrouter") if registry is not None else None
    if adapter is None or not hasattr(adapter, "_extra_status"):
        return {"available": False}

    status = adapter.status()
    stats = status.get("stats") or {}
    by_provider = stats.get("by_provider") or {}
    active = status.get("active_provider")
    active_bucket = by_provider.get(active) or {}
    avg_latency = (stats.get("avg_latency_ms") or {}).get(active)

    capabilities = {}
    try:
        capabilities = adapter.capabilities_for(active) or {}
    except Exception:
        capabilities = {}

    models: Dict[str, Any] = {}
    try:
        models = adapter.list_all_models()
    except Exception:
        models = {}

    return {
        "available": True,
        "current_provider": active,
        "current_model": status.get("default_model"),
        "streaming_enabled": status.get("enable_streaming"),
        "fallback_enabled": status.get("enable_fallback"),
        "priority": status.get("priority") or [],
        "configured_providers": status.get("configured_providers") or [],
        "unconfigured_providers": status.get("unconfigured_providers") or {},
        "inflight_requests": status.get("inflight_requests", 0),
        "latency_ms": avg_latency,
        "avg_response_time_ms": avg_latency,
        "requests": active_bucket.get("requests", 0),
        "failures": active_bucket.get("failures", 0),
        "prompt_tokens": active_bucket.get("prompt_tokens", 0),
        "completion_tokens": active_bucket.get("completion_tokens", 0),
        "total_tokens": active_bucket.get("total_tokens", 0),
        "estimated_cost_usd": active_bucket.get("estimated_cost_usd", 0.0),
        "cost_is_estimate_complete": active_bucket.get("cost_is_estimate_complete", True),
        "last_fallback": status.get("last_fallback"),
        "health": status.get("health") or {},
        "capabilities": capabilities,
        "models": models,
        "stats_by_provider": by_provider,
        "stats_by_day": stats.get("by_day") or {},
    }


def collect_tts_status(adapter_manager: "AdapterManager") -> Dict[str, Any]:
    """TTS panel - Fish Audio cloud API engine's live circuit-breaker
    health (`enabled`/`healthy`/`degraded`/`consecutive_failures`/
    `last_error`/`last_success_at`), when that engine is the one
    actually wired up. `{"available": False}` for gptsovits/f5tts/mock -
    those have no circuit breaker to report (same shape `collect_llm_
    status()` uses for "adapter not in the expected shape").

    Reads `RealFishAudioClient.tts_status()`, which itself only reads
    already-tracked in-memory counters - calling this collector (even
    repeatedly, e.g. a dashboard auto-refresh) never makes a live Fish
    Audio API request on its own (spec requirement)."""
    registry = getattr(adapter_manager, "registry", None)
    adapter = registry.get("fish_audio") if registry is not None else None
    client = getattr(adapter, "client", None)
    tts_status_fn = getattr(client, "tts_status", None)
    if tts_status_fn is None:
        return {"available": False}
    status = tts_status_fn()
    if status is None:
        return {"available": False}
    return {"available": True, "engine": "fish_audio_api", **status}


# ============================================================================
# Conversation status
# ============================================================================

_STATE_DISPLAY = {
    "sleeping": "Sleeping",
    "awakening": "Listening",
    "listening": "Listening",
    "thinking": "Thinking",
    "speaking": "Speaking",
    "waiting_user": "Waiting User",
    "idle": "Idle",
}


def collect_conversation(runtime: "Runtime", modules: Dict[str, Any]) -> Dict[str, Any]:
    """Live conversation view - session state (Sleeping/Listening/
    Thinking/Speaking/Waiting User/Idle), current request_id, session
    age. Merges `SessionManagerModule.status_snapshot()` (session state/
    timeout/wake count) with `BargeInModule.status_snapshot()` (thinking/
    speaking/current_request_id) - the two packages that, between them,
    own everything the spec's "Conversation Status" section asks for;
    neither is duplicated, this just reads both."""
    session_manager = modules.get("session_manager")
    barge_in_module = modules.get("barge_in_module")
    if session_manager is None or barge_in_module is None:
        return {"state": "unknown", "current_request_id": None, "session_id": None, "conversation_age_s": None}

    session = session_manager.status_snapshot()
    bargein = barge_in_module.status_snapshot()

    state = session.get("state", "unknown")
    if bargein.get("thinking"):
        state = "thinking"
    elif bargein.get("speaking"):
        state = "speaking"

    return {
        "state": _STATE_DISPLAY.get(state, state),
        "raw_state": state,
        "previous_state": session.get("previous_state"),
        "time_in_state_s": session.get("time_in_state_s"),
        "seconds_remaining": session.get("seconds_remaining"),
        "wake_count": session.get("wake_count"),
        "current_request_id": bargein.get("current_request_id"),
        "current_mode": bargein.get("current_mode"),
        "emergency_active": bargein.get("emergency_active"),
        "awaiting_confirmation": bargein.get("awaiting_confirmation"),
        "last_action": bargein.get("last_action"),
        "session_id": None,  # no per-session id concept exists yet (single long-lived session) - honestly null, not invented
        "conversation_age_s": session.get("time_in_state_s"),
        "config": session.get("config"),
    }


# ============================================================================
# Planner
# ============================================================================

def collect_planner(modules: Dict[str, Any]) -> Dict[str, Any]:
    """Planner view - Current Plan, Queued Plans, Running/Completed/
    Failed Tasks, Retry Count, Dependencies, Rollback State. Reuses
    `Plan.to_dict()`/`Task.to_dict()` verbatim (already carries
    `depends_on`, `attempts`, `rolled_back`, `status`) plus
    `Planner.get_status()`'s `ProgressReport` for the aggregate counts -
    the exact same two calls `/plans` already makes."""
    planner_module = modules.get("planner_module")
    if planner_module is None:
        return {"has_plan": False}

    plan_id = getattr(planner_module, "last_plan_id", None)
    if not plan_id:
        return {"has_plan": False}

    try:
        planner = planner_module.planner
        plan = planner.get_plan(plan_id)
        status = planner.get_status(plan_id)
        queue = planner.get_queue()
    except Exception as ex:
        return {"has_plan": True, "error": str(ex)}

    return {
        "has_plan": True,
        "plan": plan.to_dict(),
        "progress": {
            "plan_status": status.plan_status.value,
            "current_tasks": status.current_tasks,
            "completed_tasks": status.completed_tasks,
            "remaining_tasks": status.remaining_tasks,
            "failed_tasks": status.failed_tasks,
            "total_tasks": status.total_tasks,
            "percent_complete": status.percent_complete,
            "errors": status.errors,
            "estimated_completion": _iso(status.estimated_completion),
        },
        "queued_plan_ids": list(queue.keys()) if isinstance(queue, dict) else [],
    }


# ============================================================================
# Tool Manager
# ============================================================================

def collect_tool_manager(modules: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Tool Manager live execution view - Current Tool, Execution Time,
    Retries, Timeout, Result, History. Current/last result comes
    straight from `ToolManagerBridgeModule.last_tool`/`.last_result`
    (same fields `/tasks` prints); `history` is supplied by the caller
    (`server.py`, backed by `events_buffer`'s `tool_*` event capture -
    see that module's docstring for why history lives there instead of
    being invented here: `ToolManagerBridgeModule` itself keeps no
    history today, and this package must not add state to it)."""
    tool_manager_module = modules.get("tool_manager_module")
    if tool_manager_module is None:
        return {"current_tool": None, "last_result": None, "history": history}

    result = tool_manager_module.last_result
    return {
        "current_tool": tool_manager_module.last_tool,
        "last_result": result,
        "history": history,
    }


# ============================================================================
# Smart Home Verification (Verified Smart Home Execution sprint)
# ============================================================================

#: One row per completed verification attempt-sequence, keyed by
#: request_id - built from the SAME `history` (a flattened list of
#: `ActionVerificationStarted/Retry/Verified/Failed/Timeout` event
#: records, oldest first, from `events_buffer.verification_history()`;
#: see that module's docstring for why this lives there instead of a
#: new piece of state on any module). The three terminal event types
#: already carry the full Execution Result Model in their own `data`
#: (see `real_home_assistant.py::_result_data()`), so a row is complete
#: the moment its terminal event arrives - "started"/"retry" records
#: only ever refine `retry_count` for a request_id whose terminal event
#: hasn't shown up yet (still in flight).
_TERMINAL_VERIFICATION_TYPES = {"action_verified", "action_verification_failed", "action_verification_timeout"}


def collect_verification_status(modules: Dict[str, Any], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Smart Home Verification panel - Request ID / Entity / Service /
    Expected State / Actual State / Verification Status / Retry Count /
    Elapsed Time / Failure Reason / Final Result, one row per verified
    (or verification-attempted) Home Assistant action. Reuses the exact
    same "history supplied by the caller, backed by events_buffer"
    convention `collect_tool_manager` above already established - this
    package adds no new tracking state of its own either."""
    tool_manager_module = modules.get("tool_manager_module")
    if tool_manager_module is None:
        return {"available": False}

    rows: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for record in history:
        data = record.get("data") or {}
        request_id = data.get("request_id")
        if not request_id:
            continue
        if request_id not in rows:
            rows[request_id] = {
                "request_id": request_id, "entity_id": data.get("entity_id"),
                "service": data.get("service"), "requested_action": data.get("requested_action"),
                "expected_state": data.get("expected_state"), "actual_state": None,
                "verification_status": "in_progress", "retry_count": 0,
                "elapsed_time_ms": None, "failure_reason": None, "final_result": None,
                "started_at": record.get("received_at"),
            }
            order.append(request_id)
        row = rows[request_id]

        if record.get("type") == "action_verification_retry":
            row["retry_count"] = max(row["retry_count"], int(data.get("attempt") or 0))
            row["actual_state"] = data.get("actual_state")
        elif record.get("type") in _TERMINAL_VERIFICATION_TYPES:
            row["actual_state"] = data.get("actual_state")
            row["retry_count"] = data.get("verification_attempts", row["retry_count"])
            row["elapsed_time_ms"] = data.get("elapsed_time_ms")
            row["failure_reason"] = data.get("failure_reason")
            row["final_result"] = data.get("message")
            row["verification_status"] = {
                "action_verified": "verified",
                "action_verification_failed": "failed",
                "action_verification_timeout": "timeout",
            }[record["type"]]

    return {
        "available": True,
        "history": [rows[rid] for rid in order],
        "current_verification": rows[order[-1]] if order else None,
    }


# ============================================================================
# Vision Memory
# ============================================================================

def collect_vision_memory(search: str = "", limit: int = 50) -> Dict[str, Any]:
    """Recent observations - "Cup on desk", "Laptop open", ... No image
    display (per spec). Supports search (substring, case-insensitive)
    and returns each event's real timestamp + `importance` (the actual
    field `EventRecord` tracks - the spec calls this "confidence" but
    no such field exists anywhere in `vision_memory`; surfacing
    `importance` honestly under its real name rather than relabeling it
    "confidence" - see package docstring's "honest mapping" convention,
    same rule `real_unity.py` documents for its own field)."""
    from luno import vision_memory as vm

    try:
        events = vm.get_recent_events(limit=max(limit, 1))
        state = vm.get_world_state()
        ltm = vm.get_long_term_memory()
    except Exception as ex:
        return {"events": [], "objects": [], "long_term_memory": [], "error": str(ex)}

    needle = (search or "").strip().lower()
    event_dicts = [e.to_dict() for e in events]
    if needle:
        event_dicts = [e for e in event_dicts if needle in e["description"].lower()]

    return {
        "events": event_dicts,
        "objects": [o.to_dict() for o in state.objects.values()],
        "humans": [h.to_dict() for h in state.humans.values()],
        "room": {"light_on": state.room.light_on, "door_closed": state.room.door_closed},
        "long_term_memory": [m.to_dict() for m in ltm],
        "updated_at": _iso(state.updated_at),
    }


# ============================================================================
# Vision (Sprint 8 - real tracked-object/human-state pipeline live stats)
# ============================================================================

def collect_vision(adapter_manager: "AdapterManager", modules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Live Vision Adapter view - camera status, current FPS, objects/
    people tracked right now, frame latency, backend type ("mock"/
    "real"), tracking stats, and the most recent structured observations
    fed to Vision Memory. Reuses the SAME `VisionAdapter._extra_status()`
    fields `collect_adapters()` already surfaces generically on the
    Adapters panel (see `luno/adapters/vision.py`) - this is just a
    richer, Vision-specific view of the identical data (per-object/per-
    human detail, latest observations) rather than a second,
    independently-derived source of truth. No raw video is ever
    included here (per spec: "Display structured data only").

    `modules` (Sprint 71 - Camera Patrol, OPTIONAL, defaults to `None`)
    - when given and a `"camera_patrol_module"` key is present, this
    view is ADDITIVELY extended with `patrol_*` fields (Phase 10: "Jangan
    merombak dashboard camera yang sudah ada" - every existing caller
    that doesn't pass `modules` gets byte-for-byte the same response as
    before this sprint). Reuses `CameraPatrolModule.get_status()`
    directly - not a second, independently-tracked patrol state."""
    try:
        status_all = adapter_manager.status_all()
    except Exception as ex:
        return {"available": False, "error": str(ex)}

    entry = status_all.get("vision")
    if entry is None:
        return {"available": False, "error": "vision adapter not registered"}

    result = {
        "available": True,
        "enabled": entry.get("enabled", False),
        "module_state": entry.get("module_state", "unknown"),
        "backend": entry.get("backend", "unknown"),
        "camera_connected": entry.get("camera_connected"),
        "fps": entry.get("fps", 0.0),
        "latency_ms": entry.get("latency_ms", 0.0),
        "object_count": entry.get("object_count", 0),
        "human_count": entry.get("human_count", 0),
        "objects": entry.get("objects", []),
        "humans": entry.get("humans", []),
        "latest_observations": entry.get("latest_observations", []),
        "frames_seen": entry.get("frames_seen", 0),
        "restart_count": entry.get("restart_count", 0),
        "consecutive_failures": entry.get("consecutive_failures", 0),
        "last_error": entry.get("last_error"),
        "uptime_s": entry.get("uptime_s", 0.0),
    }

    patrol_module = (modules or {}).get("camera_patrol_module")
    if patrol_module is not None:
        try:
            status = patrol_module.get_status()
        except Exception as ex:  # pragma: no cover - defensive
            status = {"state": "unknown", "route": None, "preset": None, "index": None, "cycle": None, "max_cycles": None, "reason": str(ex)}
        result["patrol_state"] = status.get("state")
        result["patrol_route"] = status.get("route")
        result["patrol_preset"] = status.get("preset")
        result["patrol_index"] = status.get("index")
        result["patrol_cycle"] = status.get("cycle")
        result["patrol_max_cycles"] = status.get("max_cycles")
        result["patrol_reason"] = status.get("reason")

    return result


# ============================================================================
# Automation Engine (Sprint 72) - "Automation" panel
# ============================================================================

def collect_automation(modules: Dict[str, Any]) -> Dict[str, Any]:
    """Additive-only (Phase 13: "Jangan merombak dashboard architecture")
    - a NEW panel, not an extension of an existing one, since no camera/
    HA-specific dashboard view was ever meant to also own automation
    status. Reuses `AutomationEngine.get_status()` directly - not a
    second, independently-tracked automation state. If the module isn't
    registered (e.g. an older launcher wiring, or a unit test that never
    constructed it), returns `available: False` rather than raising."""
    engine = (modules or {}).get("automation_engine")
    if engine is None:
        return {"available": False, "automations": []}
    try:
        automations = engine.get_status()
    except Exception as ex:  # pragma: no cover - defensive
        return {"available": False, "error": str(ex), "automations": []}
    return {"available": True, "automations": automations}


# ============================================================================
# Proactive Intelligence (Sprint 10) - "Goals" panel
# ============================================================================

def collect_goals(modules: Dict[str, Any]) -> Dict[str, Any]:
    """Live view of `ProactiveModule.status_snapshot()` - active/queued,
    awaiting-confirmation, completed, and rejected goals, each already
    carrying its own confidence/reasoning/policy result (see
    `luno.proactive.models.Goal.to_dict()`) - this function does no
    extra shaping beyond what the module itself already returns, same
    "one source of truth, JSON-serialized" rule every other collector
    here follows."""
    proactive_module = modules.get("proactive_module")
    if proactive_module is None:
        return {"available": False, "error": "proactive module not registered"}
    try:
        snapshot = proactive_module.status_snapshot()
    except Exception as ex:
        return {"available": False, "error": str(ex)}
    snapshot["available"] = True
    return snapshot


# ============================================================================
# Memory Retrieval
# ============================================================================

def collect_memory_retrieval(modules: Dict[str, Any], query_text: str) -> Dict[str, Any]:
    """Memory Retrieval debug view - latest query, retrieved memories,
    ranking score, source, injected prompt preview, token estimate.
    Reuses `MemoryRetriever.retrieve_memories()` + `build_memory_prompt_
    block()` exactly like `/memquery` - no LLM call, same as that
    command's own docstring promises."""
    planner_module = modules.get("planner_module")
    if planner_module is None or not query_text.strip():
        return {"query": query_text, "memories": [], "prompt_block": "", "token_estimate": 0}

    from luno.memory_retrieval import build_memory_prompt_block

    retriever = planner_module.memory_retriever
    memories = retriever.retrieve_memories(query_text)
    block = build_memory_prompt_block(memories) if memories else ""
    # Rough, honestly-labeled token estimate (chars/4) - no tokenizer is
    # wired in anywhere in this project; this mirrors the same coarse
    # heuristic already used informally for "token budget" reasoning in
    # `memory_retrieval`'s own `max_tokens` config knob.
    token_estimate = round(len(block) / 4) if block else 0

    return {
        "query": query_text,
        "memories": [
            {"text": m.text, "source": m.source, "score": round(m.score, 3), "stale": m.stale}
            for m in memories
        ],
        "prompt_block": block,
        "token_estimate": token_estimate,
        "retrieval_mode": getattr(retriever.config, "retrieval_mode", None),
        "enabled": getattr(retriever.config, "enabled", None),
    }


# ============================================================================
# Decision Engine (Intelligent AI Routing Engine sprint)
# ============================================================================

def collect_routing_status(modules: Dict[str, Any], adapter_manager: "AdapterManager") -> Dict[str, Any]:
    """Decision Engine panel - spec's own field list: current config
    (`DEFAULT_PROVIDER`/`REASONING_PROVIDER`/`ENABLE_*` flags/complexity
    threshold), recent routing decisions (intent/complexity/knowledge
    source/provider chosen/why), decision counts by provider alias/
    intent/day/conversation, internet-search and knowledge-shortcut
    counts, which conversations are currently "sticky" to the reasoning
    provider, and - joined in from the LLM Manager, NOT recomputed here
    (see `luno/routing/stats.py`'s own docstring for why) - real $/token
    cost broken down by the REAL provider each routed request actually
    landed on."""
    planner_module = modules.get("planner_module")
    engine = getattr(planner_module, "decision_engine", None)
    if engine is None:
        return {"available": False}

    status = engine.status()

    registry = getattr(adapter_manager, "registry", None)
    llm_adapter = registry.get("openrouter") if registry is not None else None
    llm_cost_by_provider: Dict[str, Any] = {}
    if llm_adapter is not None and hasattr(llm_adapter, "_extra_status"):
        llm_status = llm_adapter.status()
        llm_cost_by_provider = (llm_status.get("stats") or {}).get("by_provider") or {}

    return {
        "available": True,
        "config": status["config"],
        "stats": status["stats"],
        "sticky_conversations": status["sticky_conversations"],
        "web_search_available": status["web_search_available"],
        "llm_cost_by_provider": llm_cost_by_provider,
    }


# ============================================================================
# Context (LLM Prompt Preview)
# ============================================================================

def collect_context_preview(runtime: "Runtime") -> Dict[str, Any]:
    """"LLM Prompt Preview" (Debug Mode section) - the exact context
    `ContextBuilder.build()` would hand the LLM right now, same call
    `/context` already makes. Honestly does NOT include an actual raw
    prompt string (no such thing is assembled until `OpenRouterAdapter`
    itself formats one right before the network call, which this
    package never touches) - this is the best available preview, same
    one the terminal console has always offered."""
    try:
        ctx = runtime.context_builder.build()
        return {"context": ctx.to_dict()}
    except Exception as ex:
        return {"context": {}, "error": str(ex)}


# ============================================================================
# Health page
# ============================================================================

def collect_health(runtime: "Runtime", adapter_manager: "AdapterManager") -> Dict[str, Any]:
    """Health page - CPU/RAM/GPU/Disk/SQLite/Thread Count/Queue Size/
    Average Event Latency/per-subsystem latency (where tracked)/
    Heartbeat/Warnings/Errors. CPU/RAM reuse `HeartbeatMonitor`'s own
    `psutil`-optional, honest-degrade pattern (see `core/heartbeat.py`);
    GPU/Disk are new, best-effort, equally honest-degrade additions
    (never raise, return `None` when unavailable rather than a fake
    number) - this is the one place in the whole dashboard that reads a
    genuinely NEW signal Runtime itself doesn't already expose, and it
    is read-only host telemetry, not Runtime business state."""
    report = runtime.health()
    bus_stats = runtime.event_bus.stats()
    heartbeat_stats = runtime.heartbeat.last_stats()

    cpu_percent = ram_mb = ram_percent = disk_percent = None
    try:
        import psutil  # type: ignore
        cpu_percent = psutil.cpu_percent(interval=None)
        vm_mem = psutil.virtual_memory()
        ram_mb = psutil.Process().memory_info().rss / (1024 * 1024)
        ram_percent = vm_mem.percent
        disk = psutil.disk_usage(os.getcwd())
        disk_percent = disk.percent
    except Exception:
        pass  # psutil not installed / platform unsupported - honest degrade, matches heartbeat.py

    gpu = _collect_gpu_best_effort()

    sqlite_ok = True
    sqlite_message = "reachable"
    try:
        from luno import vision_memory as vm
        vm.get_world_state()
    except Exception as ex:
        sqlite_ok = False
        sqlite_message = str(ex)

    adapter_latencies = {
        name: entry.get("avg_latency_ms")
        for name, entry in adapter_manager.status_all().items()
        if entry.get("avg_latency_ms") is not None
    }

    return {
        "overall_healthy": report.healthy,
        "modules": {
            name: {"healthy": s.healthy, "stalled": s.stalled, "message": s.message}
            for name, s in report.modules.items()
        },
        "issues": list(report.issues),
        "cpu_percent": cpu_percent,
        "ram_mb": ram_mb,
        "ram_percent": ram_percent,
        "disk_percent": disk_percent,
        "gpu": gpu,
        "sqlite_ok": sqlite_ok,
        "sqlite_message": sqlite_message,
        "thread_count": threading.active_count(),
        "queue_size": bus_stats["queue_size"],
        "avg_event_latency_ms": bus_stats["avg_latency_ms"],
        "event_throughput_per_s": heartbeat_stats.event_throughput_per_s if heartbeat_stats else None,
        "adapter_latencies_ms": adapter_latencies,
        "uptime_s": heartbeat_stats.uptime_s if heartbeat_stats else runtime.status()["uptime_s"],
    }


def _collect_gpu_best_effort() -> Optional[Dict[str, Any]]:
    """Best-effort NVIDIA GPU utilization via `nvidia-smi` (no new
    Python dependency - just shells out to a binary that may or may not
    exist). Returns `None` (never a fake value) when unavailable -
    honest-degrade, same rule as everything else in this function."""
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        first_line = result.stdout.strip().splitlines()[0]
        util, mem_used, mem_total = (p.strip() for p in first_line.split(","))
        return {"utilization_percent": float(util), "memory_used_mb": float(mem_used), "memory_total_mb": float(mem_total)}
    except Exception:
        return None


# ============================================================================
# Configuration
# ============================================================================

def collect_configuration(launcher_config: "LauncherConfig") -> Dict[str, Any]:
    """Current configuration - search, readonly values, environment
    source, precedence. API keys/tokens/secrets NEVER appear (see
    `_is_secret_key()` - checked by KEY NAME, not value, so a secret
    can't leak even if this list of "known" settings ever drifts)."""
    launcher_fields = launcher_config.to_dict()

    legacy_values: Dict[str, Any] = {}
    try:
        import luno.config as legacy_config
        for key in dir(legacy_config):
            if key.startswith("_") or not key.isupper():
                continue
            if _is_secret_key(key):
                continue
            value = getattr(legacy_config, key)
            if callable(value) or isinstance(value, type(os)):
                continue
            legacy_values[key] = value
    except Exception:
        pass

    return {
        "launcher": launcher_fields,
        "legacy_config": legacy_values,
        "precedence": [
            "1. process environment variables (always wins)",
            "2. .env file",
            "3. JSON/YAML config file (LUNO_CONFIG_FILE)",
            "4. hardcoded defaults",
        ],
        "config_file_path": launcher_config.config_file_path,
        "config_file_keys_applied": list(launcher_config.config_file_keys_applied),
        "env_file_loaded": launcher_config.env_file_loaded,
    }


# ============================================================================
# Statistics
# ============================================================================

def collect_statistics(runtime: "Runtime", modules: Dict[str, Any], stats_aggregator: Any) -> Dict[str, Any]:
    """Statistics page. Uptime/module-derived counts come straight from
    Runtime; conversation/wake/interrupt/success-rate/timing numbers are
    aggregated by `events_buffer.StatsAggregator` (a pure event-counting
    observer of the SAME Event Bus every module already publishes to -
    see that class's own docstring for exactly which event types feed
    which number, and why nothing here is invented)."""
    runtime_status = runtime.status()
    session_manager = modules.get("session_manager")
    wake_count = session_manager.status_snapshot().get("wake_count") if session_manager else None

    agg = stats_aggregator.snapshot()
    return {
        "runtime_uptime_s": runtime_status["uptime_s"],
        "wake_count": wake_count,
        **agg,
    }


# ============================================================================
# Memory Dashboard & Observability
# ============================================================================
#
# Every function below reads `luno.memory`'s PUBLIC surface only
# (`list_memories`/`get_memory`/`search_memories`/`compute_lifecycle`/
# `memory_health_report`/`analyze_memory_maintenance`/
# `preview_maintenance_text`/`list_conflicts`/`is_memory_protected`/
# `get_memory_importance`/`get_memory_retrieval_count`) - no `_memories`
# access, no `config.LONG_TERM_MEMORY_FILE` access, no second retrieval/
# ranking/consolidation logic. See docs/change_impact/memory_dashboard.md
# for the full architecture audit and reasoning. `luno.memory` is
# imported locally inside each function (not at module level) - same
# "only import what THIS function needs, avoid a module-load-order
# dependency" discipline `collect_vision_memory()` above already follows
# for `luno.vision_memory`.

_MEMORY_LIST_DEFAULT_LIMIT = 50
#: Hard ceiling - Phase 2/12's explicit "never return the whole store
#: unbounded". A dashboard page has no legitimate reason to render more
#: than a couple hundred rows at once; pagination (`offset`) is how a
#: user reaches the rest.
_MEMORY_LIST_MAX_LIMIT = 200
#: Bound passed to `search_memories()` itself when a search term is
#: given - generous enough that post-search filtering (lifecycle/
#: importance/category/source/conflict) below still has a real
#: candidate pool to work with, but still a real, honest bound (not
#: "everything").
_MEMORY_SEARCH_CANDIDATE_BOUND = 500

#: `conflict_status` filter values this dashboard actually supports -
#: grounded in what `_memories` entries really persist (see
#: change-impact doc's "schema-honest, not the brief's literal 6-option
#: list verbatim"). `"ambiguous_conflict"`/`"none"` read the entry's own
#: live `conflict_status` field; the other three read whether
#: `history[]` contains an entry with that `reason` (real, already-
#: persisted data from `update_memory()`'s own reason-stamping) - i.e.
#: "this memory was once corrected/superseded/refined", not "this
#: memory currently IS one".
_MEMORY_CONFLICT_FILTER_VALUES = ("ambiguous_conflict", "none", "correction", "temporal_change", "refinement")


def _memory_history_reasons(entry: Dict[str, Any]) -> set:
    history = entry.get("history")
    if not isinstance(history, list):
        return set()
    return {h.get("reason") for h in history if isinstance(h, dict) and h.get("reason")}


def _memory_matches_conflict_filter(entry: Dict[str, Any], conflict_status: str) -> bool:
    if not conflict_status or conflict_status in ("all", ""):
        return True
    live_status = entry.get("conflict_status")
    if conflict_status == "ambiguous_conflict":
        return live_status == "ambiguous_conflict"
    if conflict_status == "none":
        return live_status != "ambiguous_conflict"
    if conflict_status in ("correction", "temporal_change", "refinement"):
        return conflict_status in _memory_history_reasons(entry)
    return True  # unrecognized filter value - fail OPEN (show everything) rather than silently hiding every result


def _clamp_int(raw: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def collect_memory_overview() -> Dict[str, Any]:
    """Memory panel's summary cards + importance/category/source
    breakdown. `total`/`active`/`stale`/`archived`/`importance`/
    `conflicts`(-> `potential_conflicts`)/`duplicates`(->
    `potential_duplicates`)/`usage`/`needs_review`(-> `review_required`)/
    `protected`(-> `protected_core_memories`) all come straight from
    `memory_health_report()` - Phase 2's own "Jangan menghitung ulang
    health logic di dashboard" is satisfied by never recomputing any of
    those numbers here. `categories`/`sources` are a plain tally over
    `list_memories()`'s own already-public fields (not a new
    classification - every entry already carries these). `obsolete` is
    derived from `analyze_memory_maintenance()`'s own plan (Phase 2's
    "Jangan membuat planner kedua") by counting archive-recommended
    entries whose reason names obsolete/temporary wording specifically,
    distinguishing them from stale-by-age archive candidates - see the
    change-impact doc's risk #4 for why this is a soft (string-based)
    coupling, accepted for this sprint."""
    from luno import memory

    report = memory.memory_health_report()
    entries = [m for m in memory.list_memories() if isinstance(m, dict)]

    categories: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    for m in entries:
        category = m.get("category") or "other"
        categories[category] = categories.get(category, 0) + 1
        source = m.get("source") or "llm_auto"
        sources[source] = sources.get(source, 0) + 1

    plan = memory.analyze_memory_maintenance()
    obsolete = sum(
        1 for p in plan
        if p.get("action") == "archive" and "obsolete" in (p.get("reason") or "").lower()
    )

    return {
        "total": report["total"],
        "active": report["lifecycle"].get("active", 0),
        "stale": report["lifecycle"].get("stale", 0),
        "archived": report["lifecycle"].get("archived", 0),
        "importance": {str(k): v for k, v in report["importance"].items()},
        "categories": categories,
        "sources": sources,
        "conflicts": report["potential_conflicts"],
        "duplicates": report["potential_duplicates"],
        "obsolete": obsolete,
        "usage": report["usage"],
        # Memory Learning & Feedback Loop sprint - straight passthrough of
        # `memory_health_report()`'s own new buckets (Phase 2's "Jangan
        # menghitung ulang health logic di dashboard" applies here exactly
        # as it already does to every field above).
        "usefulness": report.get("usefulness", {"low": 0, "medium": 0, "high": 0}),
        "total_positive_feedback": report.get("total_positive_feedback", 0),
        "total_negative_feedback": report.get("total_negative_feedback", 0),
        "needs_review": report["review_required"],
        "protected": report["protected_core_memories"],
        # Memory Evaluation & Self-Calibration sprint (Step 11) - a small,
        # additive summary bucket over `evaluate_memory()`'s LIVE
        # (always-fresh, never-persisted) `recommendation` for every
        # current entry - deliberately NOT a new health-logic engine
        # (same "call the existing pure function, don't recompute its
        # logic" discipline every other field on this dict already
        # follows); just a tally of its already-computed output.
        "evaluation_recommendations": _memory_evaluation_recommendation_tally(entries),
    }


def _memory_evaluation_recommendation_tally(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    """Read-only helper for `collect_memory_overview()` above - counts
    `evaluate_memory()`'s `recommendation` across every current entry.
    `evaluate_memory()` itself is pure/never mutates, so calling it once
    per entry here is safe to do on every GET (Step 13's "Dashboard: ...
    no mutation from GET")."""
    from luno import memory

    tally = {r: 0 for r in memory.MEMORY_EVALUATION_RECOMMENDATIONS}
    for m in entries:
        rec = memory.evaluate_memory(m).get("recommendation", "keep")
        tally[rec] = tally.get(rec, 0) + 1
    return tally


#: Memory Learning & Feedback Loop sprint (Phase 17/18) - named sort
#: modes the Memory Dashboard's browse view can request, layered ON TOP
#: of the existing recency/search ranking below (never a second ranking
#: ALGORITHM - each mode is a simple `list.sort()` over fields
#: `luno.memory`'s own public accessors already expose). `""`/unrecognized
#: -> no reordering (falls back to the pre-existing recency/search order,
#: unchanged default behavior).
_MEMORY_LIST_SORT_MODES = (
    "most_used", "most_useful", "needs_review", "low_usefulness", "recently_reinforced",
    # Memory Evaluation & Self-Calibration sprint (Step 11) - four new,
    # additive sort modes, same "simple `list.sort()` over an already-
    # computed field" shape as every mode above (never a second ranking
    # algorithm).
    "highest_evaluation", "lowest_evaluation", "low_confidence", "recently_evaluated",
)


def collect_memory_list(
    lifecycle: str = "",
    importance: str = "",
    category: str = "",
    source: str = "",
    conflict_status: str = "",
    search: str = "",
    sort: str = "",
    limit: Any = None,
    offset: Any = None,
) -> Dict[str, Any]:
    """Memory panel's browsable/filterable list - Phase 2/5/6/12's own
    requirements: `search` (when given) is answered ENTIRELY by
    `search_memories()` (Phase 5's "Jangan implementasikan search/
    tokenizer sendiri" - no second matching algorithm here), with every
    other filter layered on top by simple attribute matching (since
    `search_memories()` itself takes no filters); with no `search`, the
    candidate set is `list_memories()`, sorted by recency (deterministic,
    reproducible across calls). Historical search hits
    (`search_memories()`'s own `"historical": True` results, superseded
    wording surfaced from `history[]`) are excluded from this CURRENT-
    memory list - Phase 3's own "Timeline harus membedakan CURRENT vs
    HISTORICAL": historical wording belongs in the detail/timeline view,
    not mixed into the main browsing list. Always bounded (`limit`
    clamped to `[1, 200]`, default 50) - never the whole store.

    Memory Learning & Feedback Loop sprint: `sort` (optional, one of
    `_MEMORY_LIST_SORT_MODES`) re-orders the already-filtered page - see
    that constant's own comment. Applied AFTER filtering, BEFORE
    pagination, so `most_used`/`most_useful`/etc. always sort the FULL
    matched set, never just one page of it."""
    from luno import memory

    limit_n = _clamp_int(limit, _MEMORY_LIST_DEFAULT_LIMIT, 1, _MEMORY_LIST_MAX_LIMIT)
    offset_n = _clamp_int(offset, 0, 0, 10_000_000)

    search = (search or "").strip()
    if search:
        candidates = memory.search_memories(search, limit=_MEMORY_SEARCH_CANDIDATE_BOUND)
        already_ranked = True
    else:
        candidates = memory.list_memories()
        already_ranked = False

    importance_n: Optional[int] = None
    if importance not in (None, "", "all"):
        try:
            importance_n = int(importance)
        except (TypeError, ValueError):
            importance_n = None

    sort = (sort or "").strip().lower()
    review_ids = set()
    if sort == "needs_review":
        # Lazily computed ONLY for this sort mode (Phase 12's own "never
        # recompute health/maintenance logic a second time" is satisfied
        # by calling the SAME `analyze_memory_maintenance()` every other
        # maintenance-aware view already uses, not a new planner).
        review_ids = {p["memory_id"] for p in memory.analyze_memory_maintenance() if p.get("action") == "review"}

    filtered: List[Dict[str, Any]] = []
    for original_index, m in enumerate(candidates):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        if m.get("historical"):
            continue  # a superseded-wording search hit, not a current memory - belongs in the detail view only
        entry_lifecycle = memory.compute_lifecycle(m)
        if lifecycle and lifecycle not in ("", "all") and entry_lifecycle != lifecycle:
            continue
        if importance_n is not None and memory.get_memory_importance(m) != importance_n:
            continue
        if category and category not in ("", "all") and m.get("category") != category:
            continue
        if source and source not in ("", "all") and m.get("source") != source:
            continue
        if not _memory_matches_conflict_filter(m, conflict_status):
            continue
        row = dict(m)
        row["lifecycle"] = entry_lifecycle
        row["importance"] = memory.get_memory_importance(m)
        row["is_protected"] = memory.is_memory_protected(m["id"])
        # Memory Learning & Feedback Loop sprint - same public-accessor-
        # only discipline every field above already follows (never reads
        # `usefulness_score`/`*_feedback_count` off `m` directly, so a
        # pre-sprint entry missing these keys still renders a correct,
        # backward-compatible default).
        row["usage_count"] = memory.get_memory_retrieval_count(m)
        row["usefulness"] = memory.get_memory_usefulness(m)
        row["positive_feedback_count"] = memory.get_memory_positive_feedback_count(m)
        row["negative_feedback_count"] = memory.get_memory_negative_feedback_count(m)
        row["needs_review"] = m["id"] in review_ids
        # Memory Evaluation & Self-Calibration sprint (Step 11) - LIVE
        # evaluation output (`evaluate_memory()` is pure - safe to call on
        # every GET, Step 13's "no mutation from GET"), plus the
        # separately-persisted `last_evaluated_at` (set only by an
        # explicit `calibrate_memory()` call, e.g. from a feedback event
        # or the dashboard's own "Recalibrate" button - may be `None` for
        # a memory that has never been explicitly calibrated yet, even if
        # `evaluate_memory()` can still compute a live score for it).
        evaluation = memory.evaluate_memory(m)
        row["evaluation_score"] = evaluation["score"]
        row["evaluation_confidence"] = evaluation["confidence"]
        row["evaluation_recommendation"] = evaluation["recommendation"]
        row["last_evaluated_at"] = memory.get_memory_last_evaluated_at(m)
        row["_original_index"] = original_index  # sort key only - stripped before returning, see below
        filtered.append(row)

    if not already_ranked:
        # Same `(timestamp, original list position)` tie-break rule
        # `_most_recently_touched_memory()` already established (1-second
        # timestamp resolution means two memories touched in the same
        # second, routine in fast succession, would otherwise tie and
        # `sort(..., reverse=True)`'s stability would silently keep
        # whichever came FIRST in `_memories` - not necessarily the one
        # actually touched last).
        filtered.sort(key=lambda r: (r.get("updated_at") or r.get("created_at") or "", r["_original_index"]), reverse=True)

    if sort == "most_used":
        filtered.sort(key=lambda r: (r["usage_count"], r["_original_index"]), reverse=True)
    elif sort == "most_useful":
        filtered.sort(key=lambda r: (r["usefulness"], r["_original_index"]), reverse=True)
    elif sort == "low_usefulness":
        filtered.sort(key=lambda r: (r["usefulness"], -r["_original_index"]))
    elif sort == "needs_review":
        filtered.sort(key=lambda r: (r["needs_review"], r["_original_index"]), reverse=True)
    elif sort == "recently_reinforced":
        filtered.sort(key=lambda r: (r.get("last_retrieved_at") or "", r["_original_index"]), reverse=True)
    elif sort == "highest_evaluation":
        filtered.sort(key=lambda r: (r["evaluation_score"], r["_original_index"]), reverse=True)
    elif sort == "lowest_evaluation":
        filtered.sort(key=lambda r: (r["evaluation_score"], -r["_original_index"]))
    elif sort == "low_confidence":
        filtered.sort(key=lambda r: (r["evaluation_confidence"], -r["_original_index"]))
    elif sort == "recently_evaluated":
        filtered.sort(key=lambda r: (r.get("last_evaluated_at") or "", r["_original_index"]), reverse=True)

    for row in filtered:
        row.pop("_original_index", None)

    total_matched = len(filtered)
    page = filtered[offset_n:offset_n + limit_n]

    return {
        "items": page,
        "total_matched": total_matched,
        "limit": limit_n,
        "offset": offset_n,
        "has_more": (offset_n + limit_n) < total_matched,
    }


def collect_memory_detail(memory_id: str) -> Dict[str, Any]:
    """Memory panel's detail view (Phase 4) - the full entry plus
    computed `lifecycle`/`is_protected`/`retrieval_count` (via the
    public accessor wrappers, never a raw-dict re-derivation), and, for
    a memory currently in an unresolved ambiguous conflict, the SIBLING
    entries from the same `conflict_group` (via `list_conflicts()`) so
    the UI can render "CURRENT / HISTORY-OR-CONFLICT" side by side
    (Phase 4's own "Jangan memilih salah satu secara diam-diam") without
    a second lookup round-trip. `history` is passed through verbatim -
    already bounded to 5 entries, already ordered oldest-first by
    `update_memory()`'s own append-only convention, never reordered or
    reinterpreted here (Phase 3's "gunakan hanya data yang benar-benar
    tersedia")."""
    from luno import memory

    entry = memory.get_memory(memory_id)
    if entry is None:
        return {"error": "not_found", "id": memory_id}

    detail = dict(entry)
    detail["lifecycle"] = memory.compute_lifecycle(entry)
    detail["is_protected"] = memory.is_memory_protected(memory_id)
    detail["importance"] = memory.get_memory_importance(entry)
    detail["retrieval_count"] = memory.get_memory_retrieval_count(entry)
    detail["history"] = entry.get("history") or []
    # Memory Learning & Feedback Loop sprint (Section 18 - explainability):
    # the detail view is the natural place for "why is the score what it
    # is" - `_explain_usefulness()`'s own text is exposed verbatim rather
    # than re-derived here, same "call the existing function, don't
    # recompute its logic" discipline every other field above follows.
    detail["usage_count"] = memory.get_memory_retrieval_count(entry)
    detail["usefulness"] = memory.get_memory_usefulness(entry)
    detail["positive_feedback_count"] = memory.get_memory_positive_feedback_count(entry)
    detail["negative_feedback_count"] = memory.get_memory_negative_feedback_count(entry)
    detail["usefulness_explanation"] = memory.get_memory_usefulness_explanation(entry)

    # Memory Evaluation & Self-Calibration sprint (Step 11/12) - the
    # detail view's "Why this score?" panel. `evaluation` is the LIVE
    # `evaluate_memory()` output (score/confidence/strengths/weaknesses/
    # recommendation, always fresh, never persisted - Step 9's own
    # confidence-vs-truth separation); `evaluation_score`/
    # `last_evaluated_at` are the separately-persisted fields
    # `calibrate_memory()` last wrote (may lag behind `evaluation["score"]`
    # if evidence has been recorded since the last explicit calibration -
    # both are exposed so the UI can show either "live" or "last
    # calibrated" as it prefers, honestly, rather than silently picking
    # one). `evidence_counts` is the same raw-counter snapshot the
    # overview tally is built from. `evaluation_explanation` is
    # `_explain_evaluation()`'s own ready-to-render text, same "expose the
    # existing explanation verbatim" discipline as `usefulness_explanation`
    # above.
    evaluation = memory.evaluate_memory(entry)
    detail["evaluation"] = evaluation
    detail["evaluation_score"] = evaluation["score"]
    detail["evaluation_confidence"] = evaluation["confidence"]
    detail["evaluation_recommendation"] = evaluation["recommendation"]
    detail["last_calibrated_evaluation_score"] = memory.get_memory_evaluation_score(entry)
    detail["last_evaluated_at"] = memory.get_memory_last_evaluated_at(entry)
    detail["evidence_counts"] = memory.get_memory_evidence_counts(entry)
    detail["evaluation_explanation"] = memory.get_memory_evaluation_explanation(entry)

    # Memory Outcome Telemetry & Closed-Loop Learning sprint (Step 14/15/
    # 16) - "Outcome" panel (a thin re-shaping of `evidence_counts`/
    # `evaluate_memory()` already computed above via the dedicated
    # `get_memory_outcome_summary()` accessor, so a caller that only
    # wants the outcome-telemetry shape doesn't need to know this
    # function's own internal field names) plus the "Why selected? / Why
    # not selected?" explanation panel. Both purely read-only.
    detail["outcome_summary"] = memory.get_memory_outcome_summary(memory_id)
    detail["selection_explanation"] = memory.get_memory_selection_explanation(entry)

    # Memory Decision Quality & Adaptive Retrieval sprint (Phase 8/9) -
    # "Context Specialization" panel: per-category evidence + derived
    # (never persisted) context score, so the UI can show WHY a memory
    # ranked where it did for a particular kind of query, distinct from
    # `evaluation`/`usefulness` above (which are GLOBAL, not per-context).
    # `None` when this memory has no context evidence recorded at all -
    # rendered as "no context-specific evidence yet", never as 0/bad.
    detail["context_specialization"] = memory.get_memory_context_specialization_summary(memory_id)

    conflict_siblings: List[Dict[str, Any]] = []
    if entry.get("conflict_status") == "ambiguous_conflict":
        for group in memory.list_conflicts():
            ids_in_group = {g.get("id") for g in group if isinstance(g, dict)}
            if memory_id in ids_in_group:
                conflict_siblings = [dict(g) for g in group if isinstance(g, dict) and g.get("id") != memory_id]
                break
    detail["conflict_siblings"] = conflict_siblings

    return detail


def collect_memory_health() -> Dict[str, Any]:
    """Passthrough of `memory_health_report()` - Phase 2's own "Gunakan
    existing memory_health_report(). Jangan menghitung ulang health
    logic di dashboard.\""""
    from luno import memory

    return memory.memory_health_report()


def collect_memory_maintenance_preview() -> Dict[str, Any]:
    """Passthrough of `analyze_memory_maintenance()` (structured plan)
    plus `preview_maintenance_text()` (the same human-readable dry-run
    text the "preview maintenance memory" voice/typed command already
    produces) - Phase 2/9's own "Gunakan existing
    analyze_memory_maintenance(). Jangan membuat planner kedua." Never
    calls `apply_maintenance_plan()` - this is the PREVIEW half of
    Phase 9's Preview -> Apply flow only."""
    from luno import memory

    return {
        "plan": memory.analyze_memory_maintenance(),
        "text": memory.preview_maintenance_text(),
    }


def collect_memory_conflicts() -> Dict[str, Any]:
    """Memory panel's "Needs Review" conflict groups (Phase 8) -
    passthrough of `list_conflicts()`, reshaped only by adding each
    entry's computed `lifecycle` (display convenience, not new
    classification)."""
    from luno import memory

    groups = []
    for group in memory.list_conflicts():
        rows = []
        for entry in group:
            if not isinstance(entry, dict):
                continue
            row = dict(entry)
            row["lifecycle"] = memory.compute_lifecycle(entry)
            rows.append(row)
        if rows:
            groups.append(rows)
    return {"groups": groups}


def collect_memory_context_leaderboard(category: str = "", order: str = "top", limit=None) -> Dict[str, Any]:
    """Memory Decision Quality & Adaptive Retrieval sprint (Phase 8/9) -
    Context Specialization panel: "which memories are consistently
    useful/poor when the conversation is about category X", a thin
    passthrough of the existing `list_context_specialized_memories()`
    (no second leaderboard/ranking implementation - this function only
    parses query-string-shaped inputs into that function's own
    parameters and reshapes the result for the UI).

    IMPORTANT (explanation, not truth - Strict Rule #11): `context_score`
    here is EVIDENCE of how a memory has performed in this category so
    far, never a claim that the memory's content is factually correct.
    Distinguish clearly in the UI from `importance`/`relevance`: a memory
    can rank #1 on this leaderboard and still be irrelevant to any given
    turn's query - relevance is decided per-turn by `assemble_context()`,
    never by this leaderboard. Does not create a second dashboard page -
    this is one more panel on the existing Memory Dashboard, alongside
    `collect_memory_overview()`/`collect_memory_list()`/
    `collect_memory_detail()`."""
    from luno import memory

    cat = category or None
    if cat is not None and cat not in memory.MANUAL_MEMORY_CATEGORIES:
        return {"error": "unknown_category", "category": category, "rows": []}
    ord_ = order if order in ("top", "bottom") else "top"
    try:
        lim = int(limit) if limit not in (None, "") else 20
    except (TypeError, ValueError):
        lim = 20
    lim = max(0, min(lim, 100))  # same "bounded, no full-dump" discipline as collect_memory_list

    rows = memory.list_context_specialized_memories(category=cat, order=ord_, limit=lim)
    return {
        "rows": rows,
        "category": cat,
        "order": ord_,
        "categories_available": list(memory.MANUAL_MEMORY_CATEGORIES),
    }


# ============================================================================
# Memory Decision Trace / Retrieval Funnel / Topic Timeline / Quality
# Metrics (Luno Brain Debugger - Memory & Voice Observability Dashboard
# sprint)
# ============================================================================
#
# OBSERVABILITY ONLY - every function below is a pure, read-only
# transformation of data `PlannerBridgeModule` already computed this turn
# (`_last_turn_trace` / `_turn_trace_history`, both built by the
# UNMODIFIED `luno.memory_turn_trace.build_turn_trace()`, itself fed from
# the real, unmodified `assemble_context()`/`retrieve_memories()` calls -
# see `main_runtime_demo.py::_handle_utterance()`). Nothing here re-runs
# retrieval, re-ranks anything, re-tokenizes a query, or calls an LLM -
# see this file's own module docstring for that same "no independently-
# derived view" discipline, now applied to per-turn memory decisions
# instead of stored memory entries.
#
# `MemoryTurnTrace` (Memory Outcome Telemetry sprint) deliberately never
# stores the user's raw utterance or the assistant's raw reply text - see
# that class's own docstring. That is ALSO this section's privacy
# boundary: no function below can show a raw query even if asked to,
# because the underlying trace was never given one to store. Only short,
# bounded labels (intent/reference-type/topic TERMS, not sentences) and
# ids/scores/counts are ever returned.

#: Bound on how many recent turns `collect_memory_turn_list()` will ever
#: return in one response - `_turn_trace_history` itself is already
#: hard-bounded (`deque(maxlen=100)`), this is a second, independent
#: bound on the RESPONSE size (same "bounded, no full-dump" discipline
#: `collect_memory_list()`/`collect_memory_context_leaderboard()` above
#: already use).
_TURN_LIST_MAX = 100


def _find_trace(modules: Dict[str, Any], turn_id: str = "", conversation_id: str = ""):
    """Shared lookup used by every collector below - never re-derives a
    trace, only searches the two bounded, already-populated structures
    `PlannerBridgeModule` maintains. Returns `(conversation_id, trace)` or
    `(None, None)`.

    Precedence: an explicit `turn_id` wins (searches the cross-
    conversation `_turn_trace_history` ring buffer - O(100) worst case,
    bounded); else an explicit `conversation_id` reads
    `_last_turn_trace[conversation_id]` directly (O(1), that dict's own
    one-per-conversation contract, unmodified by this sprint); else falls
    back to the single most recently recorded turn overall, if any."""
    planner_module = modules.get("planner_module")
    if planner_module is None:
        return None, None
    if turn_id:
        for cid, trace in reversed(planner_module._turn_trace_history):
            if getattr(trace, "turn_id", None) == turn_id:
                return cid, trace
        return None, None
    if conversation_id:
        trace = planner_module._last_turn_trace.get(conversation_id)
        return (conversation_id, trace) if trace is not None else (None, None)
    if planner_module._turn_trace_history:
        return planner_module._turn_trace_history[-1]
    return None, None


def collect_memory_turn_list(modules: Dict[str, Any], limit=None) -> Dict[str, Any]:
    """Turn picker for the Memory Debug Inspector (Phase 5) - the most
    recent turns across every conversation, most-recent-first, with just
    enough to identify one (`turn_id`, `conversation_id`, timestamp,
    intent, reference_type) - never the raw query."""
    planner_module = modules.get("planner_module")
    if planner_module is None:
        return {"turns": []}
    try:
        lim = int(limit) if limit not in (None, "") else 50
    except (TypeError, ValueError):
        lim = 50
    lim = max(0, min(lim, _TURN_LIST_MAX))
    entries = list(planner_module._turn_trace_history)[-lim:]
    return {
        "turns": [
            {
                "turn_id": getattr(trace, "turn_id", None),
                "conversation_id": cid,
                "timestamp": getattr(trace, "context_timestamp", None),
                "query_intent": getattr(trace, "query_intent", ""),
                "reference_type": getattr(trace, "reference_type", ""),
                "is_short_followup": getattr(trace, "is_short_followup", None),
                "rendered_count": len(getattr(trace, "selected_memory_ids", set())),
            }
            for cid, trace in reversed(entries)
        ],
    }


def collect_memory_decision_trace(modules: Dict[str, Any], turn_id: str = "", conversation_id: str = "", check_memory_id: str = "") -> Dict[str, Any]:
    """Phase 1's Memory Decision Trace - answers "why did Luno remember
    this?" / "why did Luno NOT remember that?" for one turn, entirely
    from `MemoryTurnTrace` fields the real production call already
    populated this turn (see `luno.memory_turn_trace.build_turn_trace()`).

    Distinguishes CANDIDATE -> SURVIVED RANKING -> SURVIVED BUDGET ->
    RENDERED per Phase 1's own explicit requirement, HONESTLY bounded by
    what this architecture's telemetry can actually tell apart today (see
    `luno/memory_turn_trace.py`'s own module docstring, section
    "Candidate / relevant / selected / rendered"): "candidate" and
    "relevant" are the same set (relevance is already gated before a
    `RelevantMemory` is ever returned), and "selected" and "rendered" are
    the same set (nothing is chosen into `AssembledContext.items` and
    then separately dropped before rendering) - so the per-candidate
    status below is exactly the two states this trace CAN honestly
    distinguish (`RENDERED` vs `NOT SELECTED (outranked or lost to
    budget/dedup)`), while the separate `funnel` block alongside it gives
    the AGGREGATE stage-by-stage counts (Phase 2) so a reader can still
    see roughly WHERE the drop-off happened in bulk. This function does
    NOT fabricate a fake per-candidate "dropped at ranking" vs "dropped
    at budget" vs "deduplicated" distinction the trace was never given
    enough information to make - see docs/change_impact/
    memory_voice_observability_dashboard.md's own "Known Limitations".

    `check_memory_id` (optional) answers Phase 1's SECOND question for
    one specific memory id not necessarily selected this turn: `RENDERED`,
    `NOT SELECTED`, `NOT DETECTED` (never became a candidate this turn -
    the classic "why did Luno NOT remember that?" case), or `NO CANDIDATE`
    (this turn had no candidates at all, of any kind)."""
    cid, trace = _find_trace(modules, turn_id=turn_id, conversation_id=conversation_id)
    if trace is None:
        return {"found": False}

    candidates = []
    for mid in sorted(trace.candidate_memory_ids):
        candidates.append({
            "memory_id": mid,
            "status": "RENDERED" if mid in trace.selected_memory_ids else "NOT SELECTED",
            "relevance": trace.retrieval_scores.get(mid),
            "reason": trace.selection_reasons.get(mid, ""),
        })
    # Verified Facts / Episodic Memory - read-only awareness only (see
    # `MemoryTurnTrace`'s own docstring), listed separately since they
    # are never part of `candidate_memory_ids`'s manual-memory universe.
    other_selected = {
        "verified_facts": sorted(trace.selected_verified_fact_ids),
        "episodic_memory": sorted(trace.selected_experience_ids),
    }

    result = {
        "found": True,
        "turn_id": trace.turn_id,
        "conversation_id": cid,
        "timestamp": trace.context_timestamp,
        "query": {
            "query_intent": trace.query_intent,
            "reference_type": trace.reference_type,
            "is_short_followup": trace.is_short_followup,
            "query_category": trace.query_category,
        },
        "topic_state": {
            "active_topic_terms": trace.active_topic_terms,
            "topic_history": trace.topic_history,
        },
        "retrieval": {
            "retrieval_called": trace.retrieval_called,
            "candidate_count": len(trace.candidate_memory_ids),
        },
        "candidates": candidates,
        "other_selected": other_selected,
        "funnel": collect_retrieval_funnel(modules, turn_id=turn_id, conversation_id=conversation_id)["funnel"],
    }

    if check_memory_id:
        if not trace.candidate_memory_ids and not trace.selected_memory_ids:
            check_status = "NO CANDIDATE"
        elif check_memory_id in trace.selected_memory_ids:
            check_status = "RENDERED"
        elif check_memory_id in trace.candidate_memory_ids:
            check_status = "NOT SELECTED"
        else:
            check_status = "NOT DETECTED"
        result["check_memory_id"] = check_memory_id
        result["check_status"] = check_status

    return result


#: Ordered funnel stages (Phase 2) - display order, not a computation
#: order (the real computation order lives entirely in
#: `memory_context.assemble_context()`, this is only the label mapping
#: for `MemoryTurnTrace.funnel`'s own dict keys).
_FUNNEL_STAGES = [
    ("query", "Query"),
    ("topic_candidates", "Topic candidates"),
    ("memory_candidates", "Memory candidates"),
    ("context_items", "ContextItems"),
    ("after_dedup", "After dedup"),
    ("after_ranking", "After ranking"),
    ("after_budget", "After budget"),
    ("prompt", "Prompt"),
]


def collect_retrieval_funnel(modules: Dict[str, Any], turn_id: str = "", conversation_id: str = "") -> Dict[str, Any]:
    """Phase 2's visual funnel - stage counts ONLY, straight from
    `MemoryTurnTrace.funnel` (itself a straight copy of
    `assemble_context(funnel=...)`'s own write-only output - see that
    parameter's own docstring). A missing stage key means "not measured
    this turn" (e.g. `assemble_context()` returned early for a signal-
    less query, before any of the later stages ever ran) - reported as
    `None`, never fabricated as `0`."""
    _, trace = _find_trace(modules, turn_id=turn_id, conversation_id=conversation_id)
    if trace is None:
        return {"found": False, "funnel": []}
    counts = trace.funnel or {}
    return {
        "found": True,
        "funnel": [
            {"stage": key, "label": label, "count": counts.get(key)}
            for key, label in _FUNNEL_STAGES
        ],
    }


def collect_topic_history_timeline(modules: Dict[str, Any], conversation_id: str = "") -> Dict[str, Any]:
    """Phase 3's topic-history timeline for one conversation - built
    entirely from each recorded turn's OWN `topic_history` snapshot
    (captured by `build_turn_trace()` at the START of that turn, before
    that turn's own update - see `MemoryTurnTrace.topic_history`'s own
    docstring) plus that turn's `active_topic_terms`, walked across every
    turn for this conversation still present in the bounded
    `_turn_trace_history` ring buffer (older turns are simply not
    available once evicted - honestly reported via `"turns_available"`,
    never backfilled/estimated). Also includes the LIVE current
    `_active_topic`/`_topic_history` state as a final `"current"` entry,
    so a reader can see what the conversation's topic state is RIGHT NOW,
    not just what it was at each captured turn."""
    planner_module = modules.get("planner_module")
    if planner_module is None or not conversation_id:
        return {"turns": [], "current": None}

    turns = []
    for cid, trace in planner_module._turn_trace_history:
        if cid != conversation_id:
            continue
        turns.append({
            "turn_id": trace.turn_id,
            "timestamp": trace.context_timestamp,
            "reference_type": trace.reference_type,
            "is_short_followup": trace.is_short_followup,
            "active_topic_terms": trace.active_topic_terms,
            "topic_history": trace.topic_history,
        })

    current_active = planner_module._active_topic.get(conversation_id)
    current_history = planner_module._topic_history.get(conversation_id) or []
    current = {
        "active_topic_terms": sorted(current_active.terms) if current_active else [],
        "active_topic_age": current_active.turns_since_active if current_active else None,
        "topic_history": [
            {"terms": sorted(e.terms), "age": e.turns_since_active}
            for e in current_history
        ],
    }
    return {"turns": turns, "turns_available": len(turns), "current": current}


def collect_observability_summary(modules: Dict[str, Any], conversation_id: str = "") -> Dict[str, Any]:
    """Sprint 50 (Runtime Observability) - the "at minimum" fields the
    sprint brief's own Phase 5 asks a dashboard panel to show, built
    ENTIRELY from the SAME already-populated `MemoryTurnTrace` every
    other collector in this section reads (`_find_trace()` - no new
    computation, no new store). Covers: reference classification, topic
    decision (including the Sprint 50 `topic_decision`/
    `ambiguity_check_result`/`is_ambiguity_refusal` fields added this
    sprint), selected-candidate counts, funnel, and any errors this
    turn's own `MemoryTurnTrace` construction hit (best-effort - see
    `_handle_utterance()`'s own try/except around `build_turn_trace()`;
    this collector cannot see a swallowed exception's message, only that
    no trace exists for a turn that should have one).

    Deliberately does NOT surface the raw "latest user input"/"latest
    assistant response" TEXT the brief's own Phase 5 example also lists -
    `MemoryTurnTrace` has never stored raw conversation text (see that
    module's own long-standing privacy boundary, restated in every prior
    observability sprint), and this collector does not create an
    exception to it. That raw text is already visible elsewhere in this
    SAME dashboard, unredacted, via the pre-existing generic Event Bus/
    Logs pages (`speech_recognized`/`assistant_response` events already
    carry a `text` field - see `events_buffer.py`/`logs_buffer.py`) - a
    reader who needs both the text and this bounded summary side-by-side
    already can, without this collector duplicating privacy-sensitive
    state into a second place."""
    cid, trace = _find_trace(modules, conversation_id=conversation_id)
    if trace is None:
        return {"found": False}
    return {
        "found": True,
        "turn_id": trace.turn_id,
        "conversation_id": cid,
        "timestamp": trace.context_timestamp,
        "reference_classification": {
            "reference_type": trace.reference_type,
            "is_short_followup": trace.is_short_followup,
            "query_intent": trace.query_intent,
        },
        "topic_decision": {
            "decision": trace.topic_decision,
            "active_topic_terms": trace.active_topic_terms,
            "ambiguity_check_result": trace.ambiguity_check_result,
            "ambiguity_refusal": trace.is_ambiguity_refusal,
        },
        "selection": {
            "candidate_count": len(trace.candidate_memory_ids),
            "selected_count": len(trace.selected_memory_ids),
            "funnel": trace.funnel,
        },
        "status": (
            "REFUSED" if trace.is_ambiguity_refusal
            else "PASS" if (trace.candidate_memory_ids or trace.topic_decision not in ("", "NO_CANDIDATE"))
            else "NO_CANDIDATE"
        ),
    }


def collect_session_trace(modules: Dict[str, Any], conversation_id: str = "", limit=None) -> Dict[str, Any]:
    """Sprint 50 Phase 6's bounded session/conversation trace - for each
    recorded turn in `_turn_trace_history` belonging to `conversation_id`,
    renders the fixed 8-stage pipeline diagram the sprint brief's own
    Phase 6 asks for (USER INPUT -> CLASSIFICATION -> REFERENCE
    RESOLUTION -> TOPIC UPDATE -> MEMORY CANDIDATES -> MEMORY SELECTION ->
    CONTEXT ASSEMBLY -> ASSISTANT RESPONSE) as plain per-stage labels -
    NOT a new state machine, purely a formatting pass over fields
    `MemoryTurnTrace` already carries. Already bounded by
    `_turn_trace_history`'s own pre-existing `deque(maxlen=100)` (Sprint
    32) - this function adds no new storage, just reads it filtered by
    conversation and optionally re-bounded further by `limit`."""
    planner_module = modules.get("planner_module")
    if planner_module is None:
        return {"turns": []}
    try:
        lim = int(limit) if limit not in (None, "") else 20
    except (TypeError, ValueError):
        lim = 20
    lim = max(0, min(lim, 100))
    matching = [(cid, t) for cid, t in planner_module._turn_trace_history if not conversation_id or cid == conversation_id]
    matching = matching[-lim:]
    turns = []
    for cid, t in matching:
        turns.append({
            "turn_id": t.turn_id,
            "conversation_id": cid,
            "timestamp": t.context_timestamp,
            "pipeline": [
                {"stage": "USER_INPUT", "detail": "(text not stored here - see Event Bus/Logs pages)"},
                {"stage": "CLASSIFICATION", "detail": f"query_intent={t.query_intent or '(none)'}"},
                {"stage": "REFERENCE_RESOLUTION", "detail": f"reference_type={t.reference_type or '(none)'}, is_short_followup={t.is_short_followup}"},
                {"stage": "TOPIC_UPDATE", "detail": f"topic_decision={t.topic_decision or '(none)'}, ambiguity_refusal={t.is_ambiguity_refusal}"},
                {"stage": "MEMORY_CANDIDATES", "detail": f"candidate_count={len(t.candidate_memory_ids)}"},
                {"stage": "MEMORY_SELECTION", "detail": f"selected_count={len(t.selected_memory_ids)}"},
                {"stage": "CONTEXT_ASSEMBLY", "detail": f"funnel={t.funnel}"},
                {"stage": "ASSISTANT_RESPONSE", "detail": "(text not stored here - see Event Bus/Logs pages)"},
            ],
        })
    return {"turns": turns, "turns_available": len(turns)}


def collect_memory_quality_metrics(modules: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 4's aggregate memory quality metrics - computed ENTIRELY
    over the turns still present in the bounded `_turn_trace_history`
    ring buffer (at most the last 100 turns, across every conversation -
    NOT "all time", honestly labeled via `"sample_size"`). Never
    fabricates a metric this telemetry cannot actually support - see
    each field's own comment for exactly what it measures and, where
    applicable, why a field is `"Unavailable / telemetry not
    instrumented"` instead of a number."""
    planner_module = modules.get("planner_module")
    if planner_module is None:
        return {"sample_size": 0}

    traces = [t for _cid, t in planner_module._turn_trace_history]
    n = len(traces)
    if n == 0:
        return {"sample_size": 0}

    retrieval_called = [t for t in traces if t.retrieval_called]
    empty_retrieval = [t for t in retrieval_called if not t.candidate_memory_ids]
    topic_candidate_turns = [t for t in traces if t.topic_history and any(e.get("produced_candidate") for e in t.topic_history)]
    topic_reference_turns = [t for t in traces if t.reference_type and t.reference_type != "unknown"]
    candidate_counts = [len(t.candidate_memory_ids) for t in traces]
    rendered_counts = [len(t.selected_memory_ids) for t in traces]

    intent_distribution: Dict[str, int] = {}
    for t in traces:
        key = t.query_intent or "(unclassified)"
        intent_distribution[key] = intent_distribution.get(key, 0) + 1

    reference_type_distribution: Dict[str, int] = {}
    for t in traces:
        key = t.reference_type or "(unclassified)"
        reference_type_distribution[key] = reference_type_distribution.get(key, 0) + 1

    def _pct(numer: int, denom: int) -> Optional[float]:
        return round(100.0 * numer / denom, 1) if denom else None

    def _avg(values: List[int]) -> Optional[float]:
        return round(sum(values) / len(values), 2) if values else None

    return {
        "sample_size": n,
        "retrieval_hit_rate_pct": _pct(len(retrieval_called) - len(empty_retrieval), len(retrieval_called)),
        "empty_retrieval_rate_pct": _pct(len(empty_retrieval), len(retrieval_called)),
        # "Topic continuity hit rate" - of turns that carried a bounded
        # topic-history entry marked as having produced a candidate this
        # turn (the actual Phase 6 mechanism from the Memory Topic
        # Retention sprint - see `select_topic_candidates()`), how many
        # there were, as a share of ALL turns in this sample.
        "topic_continuity_hit_rate_pct": _pct(len(topic_candidate_turns), n),
        "topic_candidate_hit_rate_pct": _pct(len(topic_candidate_turns), n),
        # Contamination requires knowing whether an injected topic was
        # ACTUALLY wrong for that turn - a judgment call this telemetry
        # has no ground truth for (it would require either a human label
        # or a second judge, both explicitly forbidden this sprint) -
        # honestly reported as unavailable rather than guessed.
        "topic_contamination_rate": "Unavailable / telemetry not instrumented",
        "average_candidate_count": _avg(candidate_counts),
        "average_rendered_context_item_count": _avg(rendered_counts),
        # No token/byte size is captured anywhere in `MemoryTurnTrace` -
        # only item COUNTS - so "average prompt context size" and "budget
        # utilization" (which need actual token/byte totals against
        # `MemoryRetrievalConfig.max_tokens`) cannot be honestly reported
        # as numbers without recomputing them from raw text this module
        # deliberately never stores (see module docstring). Reported as
        # unavailable rather than approximated from item counts alone.
        "average_prompt_context_size": "Unavailable / telemetry not instrumented",
        "budget_utilization": "Unavailable / telemetry not instrumented",
        "intent_distribution": intent_distribution,
        "reference_type_distribution": reference_type_distribution,
        # "Topic switches" - turns where a NEW topic-history entry was
        # captured (i.e. this turn was NOT a pure follow-up, so
        # `update_topic_history()` would have pushed a fresh entry - see
        # that function's own docstring). Approximated here from
        # `is_short_followup is False`, the same flag the real production
        # path itself uses to decide push-vs-preserve - not a second,
        # independently-derived heuristic.
        "topic_switches": sum(1 for t in traces if t.is_short_followup is False),
        "topic_returns_or_references_count": len(topic_reference_turns),
    }


# ─────────────────────────────────────────────
# Phase 6/7 - Voice Pipeline Observability + Latency Timeline. Both
# collectors below take an already-built `VoiceLatencyRecorder` (see
# `voice_latency.py`'s own docstring for why it is a passive Event Bus
# observer, never a re-timer) plus the dashboard's own already-existing
# `LogCapture`, and do nothing but format what those two already
# captured - no new measurement happens in this file.
# ─────────────────────────────────────────────

def collect_voice_pipeline(recorder: Any, log_capture: Any, request_id: str = "") -> Dict[str, Any]:
    """Phase 6: per-turn voice pipeline observability. With `request_id`
    given, returns that turn's timeline (coarse, from `recorder`) plus a
    per-chunk breakdown (fine-grained, parsed out of already-structured
    log lines via `voice_latency.parse_chunk_timeline_from_logs`).
    Without one, returns the most recent turn's timeline - mirrors
    `_find_trace()`'s own "most recent if unspecified" convention above."""
    if recorder is None:
        return {"found": False, "reason": "voice latency recorder not available"}

    if not request_id:
        recent = recorder.list_recent(limit=1)
        if not recent:
            return {"found": False, "reason": "no voice pipeline activity recorded yet"}
        timeline = recent[0]
        request_id = timeline["request_id"]
    else:
        timeline = recorder.snapshot_for(request_id)
        if timeline is None:
            return {"found": False, "reason": f"no telemetry recorded for request_id={request_id!r}"}

    chunks: List[Dict[str, Any]] = []
    if log_capture is not None:
        try:
            from .voice_latency import parse_chunk_timeline_from_logs
            log_entries = log_capture.snapshot(limit=2000, request_id=request_id)
            chunks = parse_chunk_timeline_from_logs(log_entries, request_id)
        except Exception:
            chunks = []  # best-effort - a parsing hiccup must never break the panel

    return {
        "found": True,
        "request_id": request_id,
        "conversation_id": timeline.get("conversation_id"),
        "streaming_enabled": timeline.get("streaming_enabled"),
        "pipelined_playback_enabled": timeline.get("pipelined_playback_enabled"),
        "cancelled": timeline.get("cancelled", False),
        "pause_count": timeline.get("pause_count", 0),
        "resume_count": timeline.get("resume_count", 0),
        "chunk_count": timeline.get("chunk_count"),
        "latencies_ms": {
            "llm_first_token": timeline.get("llm_first_token_latency_ms"),
            "llm_total": timeline.get("llm_total_latency_ms"),
            "first_audio": timeline.get("first_audio_latency_ms"),
            "playback_duration": timeline.get("playback_duration_ms"),
            "total_turn": timeline.get("total_turn_latency_ms"),
        },
        "llm_execution_time_ms": timeline.get("llm_execution_time_ms"),
        "chunks": chunks,
    }


def collect_voice_latency_timeline(recorder: Any, limit=None) -> Dict[str, Any]:
    """Phase 7: bar-chart-style timeline data across recent turns - a
    list of already-derived per-turn latency breakdowns (see
    `collect_voice_pipeline` above for what each field means), suitable
    for a client-side bar chart distinguishing LLM/TTS/playback/gap time
    per turn without the dashboard guessing at causes."""
    if recorder is None:
        return {"turns": [], "turns_available": 0}
    try:
        n = int(limit) if limit else 50
    except (TypeError, ValueError):
        n = 50
    n = max(1, min(n, 200))
    recent = recorder.list_recent(limit=n)
    turns = []
    for t in recent:
        turns.append({
            "request_id": t.get("request_id"),
            "conversation_id": t.get("conversation_id"),
            "cancelled": t.get("cancelled", False),
            "pause_count": t.get("pause_count", 0),
            "latencies_ms": {
                "llm_first_token": t.get("llm_first_token_latency_ms"),
                "llm_total": t.get("llm_total_latency_ms"),
                "first_audio": t.get("first_audio_latency_ms"),
                "playback_duration": t.get("playback_duration_ms"),
                "total_turn": t.get("total_turn_latency_ms"),
            },
        })
    return {"turns": turns, "turns_available": len(turns)}
