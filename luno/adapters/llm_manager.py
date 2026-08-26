"""
llm_manager.py
==============

`LLMManagerAdapter` - Multi-LLM Provider System sprint. Replaces
`luno.adapters.openrouter.OpenRouterAdapter` as the module
`bootstrap/adapters.py` actually constructs and registers (that other
class, and its own tests, are untouched - see that module's docstring
for why both continue to exist). Same architecture diagram as before,
generalized:

    User -> Planner -> NeedLLMResponse -> LLM Manager -> Selected Provider Adapter -> Assistant Response

Event contract is BYTE-IDENTICAL to `OpenRouterAdapter`'s
(`NeedLLMResponse`/`CancelLLMRequest`/`ReloadModel`/`ConversationReset`
in; `LLMStarted`/`LLMStreaming`/`LLMChunk`/`LLMFinished`/`LLMCancelled`/
`LLMError`/`AssistantResponse` out - see `events.py`) - Planner,
Behavior Tree, Memory Retrieval, Dashboard, and Runtime don't change a
single line for this sprint. `data["provider"]` is ADDED (never
required) to `LLMFinished`/`AssistantResponse`/`LLMChunk`/`LLMStreaming`
so anything that DOES want to know which provider answered (Dashboard,
logs) can, without every existing subscriber needing to read it.

Module id / dependency-graph note: this class registers under
`name = "openrouter"` - the exact same id `OpenRouterAdapter` used,
deliberately kept (NOT renamed to e.g. `"llm_manager"`). That id is an
internal identifier other modules' `dependencies=[...]` lists,
`bootstrap/health.py`'s checks, and the Dashboard's generic adapter
table already reference by string in a dozen-plus places untouched by
this sprint - renaming it would be a purely cosmetic change with real
risk (every one of those string references would need to move in
lockstep) for zero behavioral benefit. It does NOT violate "no module
except the LLM Manager should know which provider is active" - that
requirement is about BEHAVIOR (nothing outside this file branches on
provider identity), not about what string names the Module in the
dependency graph. The Dashboard's dedicated `/api/llm` panel (see
`dashboard/collectors.py::collect_llm_status()`) is where "Current
Provider" etc. actually get surfaced to a human.

Fallback semantics (spec: "Automatic Fallback" / "Provider Priority"):
tries `LLMManagerConfig.priority` in order, skipping any provider with
no usable client (`ProviderNotConfiguredError` at `initialize()` time -
see `luno.adapters.llm.errors`). A provider's failure is fallback-
eligible unless: fallback is disabled (`ENABLE_FALLBACK=false`), it's
the LAST configured provider in the list, the error itself says
`fallback_eligible=False` (a malformed-response/stream error - retrying
elsewhere would very likely repeat it, and for streaming specifically,
content may already have been spoken/displayed downstream), or it's an
invalid-request error and `LLM_FALLBACK_ON_INVALID_REQUEST` wasn't
explicitly opted into. Every fallback transition publishes
`ProviderFallbackActivated` and is logged with the classified reason.

Per-request provider override (Intelligent AI Routing Engine sprint):
`data["provider"]` is now ALSO read (never required) on `NeedLLMResponse`
itself - an optional per-request hint (set by `luno.routing.
DecisionEngine` via `PlannerBridgeModule`, never by this file) that
tries that one provider FIRST for that one request, then falls back
through the normally configured priority order exactly as any other
failure would - see `_priority_order(requested_provider=...)`. This
never mutates `self.manager_config` (the GLOBAL active provider/
priority), never skips fallback/health/cost-tracking, and is fully
backward compatible: absent (every caller before this sprint), behavior
is byte-identical to before.

Backward compatibility: `.client` (a small shim exposing the exact
`.chat_completion(model=, messages=, ...)` shape `luno.memory.
summarize_and_archive_session()`/`PlannerBridgeModule.
_classify_device_intent()` already duck-type against) and
`.default_model` are kept as properties so `bootstrap/adapters.py`'s
`register_device_intent_classifier()`/`register_session_summary_client()`
keep working with only their `isinstance(..., MockOpenRouterClient)`
mock-detection guard swapped for `.is_mock_active_provider` (see that
file) - neither call site needed to change shape.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from .base import BaseAdapter
from .events import (
    AssistantResponse,
    CancelLLMRequest,
    ConversationReset,
    LLMCancelled,
    LLMChunk,
    LLMError,
    LLMFinished,
    LLMStarted,
    LLMStreaming,
    NeedLLMResponse,
    ProviderFallbackActivated,
    ProviderHealthChanged,
    ProviderSwitched,
    ReloadModel,
)
from .llm.config import PROVIDER_NAMES, LLMManagerConfig, build_provider_client, build_provider_config
from .llm.errors import ProviderAPIError, ProviderAuthError, ProviderInvalidRequestError, ProviderNotConfiguredError
from .llm.models import ChatResult, HealthState, ProviderHealth
from .llm.stats import LLMStats, estimate_cost
from .utils import elapsed_ms, log


class _Cancelled(Exception):
    """Internal-only control-flow signal - never published, never
    leaves `_run_request()`. Mirrors `luno.adapters.openrouter`'s own
    identically-named/purposed class."""


@dataclass
class _InFlight:
    request_id: str
    conversation_id: Optional[str]
    correlation_id: Optional[str]
    provider: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    #: set by `_activate_fallback()` the moment ANY fallback transition
    #: happens for this request - OpenAI-Primary/DeepSeek-Fallback sprint's
    #: "expose fallback=true/false per request" diagnostics requirement
    #: (see `LLMFinished`'s `data["fallback"]` and the finished log line).
    used_fallback: bool = False


class _LegacyClientShim:
    """See module docstring's "Backward compatibility" section. Only
    ever used by the two pre-existing, opportunistic, non-streaming
    reuse call sites - never by `LLMManagerAdapter` itself."""

    def __init__(self, manager: "LLMManagerAdapter") -> None:
        self._manager = manager

    def chat_completion(self, model: Optional[str] = None, messages: Optional[List[Dict[str, str]]] = None, max_tokens: Optional[int] = None, **kwargs: Any) -> ChatResult:
        return self._manager.chat_once(
            messages or [], model=model, max_tokens=max_tokens,
            system_prompt=kwargs.get("system_prompt"), temperature=kwargs.get("temperature"),
            metadata=kwargs.get("metadata"), provider=kwargs.get("provider"),
        )


class LLMManagerAdapter(BaseAdapter):
    name = "openrouter"

    def __init__(
        self,
        manager_config: Optional[LLMManagerConfig] = None,
        clients: Optional[Dict[str, Any]] = None,
        request_workers: int = 4,
    ) -> None:
        super().__init__()
        self.manager_config = manager_config or LLMManagerConfig.from_env()
        self._clients: Dict[str, Any] = dict(clients) if clients else {}
        #: provider name -> why it's currently unusable (never initialized,
        #: or `ProviderNotConfiguredError` at `initialize()` time). Absence
        #: from this dict is exactly "usable" - see `_usable_client()`.
        self._client_errors: Dict[str, str] = {}
        self._health: Dict[str, ProviderHealth] = {}
        self.stats = LLMStats()
        self._inflight: Dict[str, _InFlight] = {}
        self._inflight_lock = threading.RLock()
        self._request_workers = max(1, request_workers)
        self._request_executor: Optional[ThreadPoolExecutor] = None
        self._health_thread: Optional[threading.Thread] = None
        self._health_stop = threading.Event()
        #: most recent `ProviderFallbackActivated` payload, if any -
        #: spec's "Dashboard displays: ... Fallback Status" (see
        #: `_extra_status()`/`dashboard/collectors.py::collect_llm_status()`).
        #: `None` means "no fallback has happened since this adapter started".
        self._last_fallback: Optional[Dict[str, Any]] = None
        self._build_missing_clients()

    def _build_missing_clients(self) -> None:
        """Builds (never `initialize()`s - that's `_do_start()`'s job,
        so a freshly-constructed-but-not-started adapter never opens a
        real connection) every provider NOT already supplied via the
        `clients=` constructor arg (tests inject `MockProviderClient`s
        this way). All five are always built regardless of which one is
        currently active - Runtime Switching needs every OTHER
        provider's client already standing by, not built lazily on
        first switch."""
        for name in PROVIDER_NAMES:
            if name in self._clients:
                continue
            try:
                pcfg = build_provider_config(
                    name, default_timeout_s=self.manager_config.timeout_s,
                    default_max_retries=self.manager_config.max_retries,
                )
                self._clients[name] = build_provider_client(name, pcfg)
            except Exception as ex:
                self._client_errors[name] = str(ex)

    # -- lifecycle ----------------------------------------------------------

    def _do_start(self) -> None:
        self._request_executor = ThreadPoolExecutor(max_workers=self._request_workers, thread_name_prefix="luno-llm-req")
        for name, client in list(self._clients.items()):
            try:
                client.initialize()
                self._client_errors.pop(name, None)
            except ProviderNotConfiguredError as ex:
                self._client_errors[name] = str(ex)
            except Exception as ex:
                self._client_errors[name] = str(ex)
                log(f"provider '{name}' failed to initialize: {ex}", self.name)
        active_ok = self.manager_config.provider not in self._client_errors
        log(
            f"LLM Manager started (active provider='{self.manager_config.provider}', ok={active_ok}, "
            f"priority={self.manager_config.priority}, configured={self._configured_provider_names()})",
            self.name,
        )
        self._health_stop.clear()
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True, name="luno-llm-health")
        self._health_thread.start()

    def _do_stop(self) -> None:
        self._health_stop.set()
        thread, self._health_thread = self._health_thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._inflight_lock:
            for entry in self._inflight.values():
                entry.cancel_event.set()
            self._inflight.clear()
        pool, self._request_executor = self._request_executor, None
        if pool is not None:
            pool.shutdown(wait=False)
        for client in self._clients.values():
            try:
                client.shutdown()
            except Exception:
                pass

    # -- health polling (spec: "Provider Health - Continuously monitor") --------

    def _health_loop(self) -> None:
        while not self._health_stop.is_set():
            self._poll_health_once()
            self._health_stop.wait(max(1.0, self.manager_config.health_poll_interval_s))

    def _poll_health_once(self) -> None:
        for name in self._configured_provider_names():
            client = self._clients.get(name)
            if client is None:
                continue
            try:
                new_health = client.health()
            except Exception as ex:
                new_health = ProviderHealth(name, HealthState.OFFLINE, str(ex))
            old = self._health.get(name)
            self._health[name] = new_health
            if old is not None and old.state != new_health.state:
                log(f"provider '{name}' health: {old.state.value} -> {new_health.state.value} ({new_health.message})", self.name)
                self.publish(ProviderHealthChanged(data={
                    "provider": name, "from_state": old.state.value, "to_state": new_health.state.value,
                    "message": new_health.message,
                }))

    def poll_health_now(self) -> Dict[str, Any]:
        """Synchronous, on-demand health check - used by the Dashboard's
        `/api/llm` endpoint (see `dashboard/collectors.py`) so a human
        doesn't have to wait for the next background poll tick to see a
        just-fixed provider go green."""
        self._poll_health_once()
        return self.provider_health_all()

    def provider_health_all(self) -> Dict[str, Any]:
        return {name: h.to_dict() for name, h in self._health.items()}

    def _configured_provider_names(self) -> List[str]:
        return [n for n in PROVIDER_NAMES if n not in self._client_errors and n in self._clients]

    def _usable_client(self, name: str) -> Optional[Any]:
        if name in self._client_errors:
            return None
        return self._clients.get(name)

    def _priority_order(self, requested_provider: Optional[str] = None) -> List[str]:
        """The configured priority order, unless `requested_provider` is
        given AND usable - in that case it's moved to the FRONT for this
        one call (fallback still walks the rest of the list afterward in
        its normal order). Never mutates `self.manager_config` - this is
        purely a per-request view, so the adapter's global active
        provider/priority is completely unaffected by any number of
        per-request overrides (Intelligent AI Routing Engine sprint -
        "no module except the LLM Manager should know which provider is
        active" still holds: the GLOBAL active provider never changes
        because of a routed request)."""
        base = [p for p in self.manager_config.priority if self._usable_client(p) is not None]
        if not requested_provider or requested_provider not in base:
            return base
        return [requested_provider] + [p for p in base if p != requested_provider]

    # -- event dispatch (identical contract to OpenRouterAdapter) ---------------

    def handle_event(self, event: Any) -> None:
        if event.type == NeedLLMResponse.EVENT_TYPE:
            self._handle_need_llm_response(event)
        elif event.type == CancelLLMRequest.EVENT_TYPE:
            self._handle_cancel(event)
        elif event.type == ReloadModel.EVENT_TYPE:
            self._handle_reload_model(event)
        elif event.type == ConversationReset.EVENT_TYPE:
            self._handle_conversation_reset(event)

    # -- NeedLLMResponse ----------------------------------------------------

    def _handle_need_llm_response(self, event: Any) -> None:
        request_id = event.get("request_id") or event.event_id
        conversation_id = event.get("conversation_id")
        correlation_id = event.get("correlation_id") or request_id
        model = event.get("model") or self.manager_config.default_model
        messages = event.get("messages") or []

        # Intelligent AI Routing Engine sprint - OPTIONAL per-request
        # provider override. Absent (every caller before this sprint,
        # and every caller that doesn't opt in), behavior is byte-
        # identical to before: `_priority_order(None)` returns exactly
        # what `_priority_order()` always returned. When set (by
        # `luno.routing.DecisionEngine` via `PlannerBridgeModule`, see
        # that package's docstring), this request tries the requested
        # provider FIRST, then falls back through the normal configured
        # priority order exactly as any other failure would - this
        # never bypasses fallback/health/cost-tracking, it only changes
        # which provider is tried first for THIS one request. An
        # unusable/unknown override is logged and silently ignored
        # (fails open to the normal configured order), never an error.
        requested_provider = (event.get("provider") or "").strip().lower() or None
        if requested_provider and requested_provider not in self._configured_provider_names():
            log(f"request {request_id}: requested provider override '{requested_provider}' is not usable - ignoring, using configured priority order", self.name)
            requested_provider = None
        elif requested_provider:
            log(f"request {request_id}: provider override -> '{requested_provider}'", self.name)

        system_prompt = event.get("system_prompt")
        temperature = event.get("temperature")
        max_tokens = event.get("max_tokens")
        stream = event.get("stream")
        if stream is None:
            stream = self.manager_config.enable_streaming
        metadata = event.get("metadata") or {}

        entry = _InFlight(request_id=request_id, conversation_id=conversation_id, correlation_id=correlation_id)
        with self._inflight_lock:
            self._inflight[request_id] = entry

        pool = self._request_executor
        if pool is None:
            log(f"request {request_id}: adapter not started, dropping", self.name)
            with self._inflight_lock:
                self._inflight.pop(request_id, None)
            return
        pool.submit(self._run_request, entry, model, messages, system_prompt, temperature, max_tokens, bool(stream), metadata, requested_provider)

    def _run_request(
        self, entry: _InFlight, model: Optional[str], messages: List[Dict[str, str]],
        system_prompt: Optional[str], temperature: Optional[float], max_tokens: Optional[int],
        stream: bool, metadata: Dict[str, Any], requested_provider: Optional[str] = None,
    ) -> None:
        ids = {"request_id": entry.request_id, "conversation_id": entry.conversation_id, "correlation_id": entry.correlation_id}
        t0 = time.time()
        try:
            if entry.cancel_event.is_set():
                return

            log(f"request {entry.request_id} started (stream={stream}, messages={len(messages)})", self.name)
            self.publish(LLMStarted(data={**ids, "model": model, "stream": stream}))

            try:
                if stream:
                    text, meta, provider_used = self._run_streaming_with_fallback(entry, ids, model, messages, system_prompt, temperature, max_tokens, metadata, requested_provider)
                else:
                    text, meta, provider_used = self._run_non_streaming_with_fallback(entry, model, messages, system_prompt, temperature, max_tokens, metadata, requested_provider)
            except _Cancelled:
                return
            except ProviderAPIError as ex:
                self._publish_error(ids, model, ex)
                self.stats.record(
                    provider=entry.provider or self.manager_config.provider, conversation_id=entry.conversation_id,
                    prompt_tokens=None, completion_tokens=None, cost=None, latency_ms=elapsed_ms(t0), failed=True,
                )
                return
            except Exception as ex:  # never let anything escape this thread uncaught
                log(f"request {entry.request_id}: unexpected error: {ex}", self.name)
                self._publish_error(ids, model, ex)
                return

            elapsed = elapsed_ms(t0)
            usage = meta.get("usage") or {}
            finish_reason = meta.get("finish_reason")
            model_used = meta.get("model") or model or provider_used
            # Diagnostics requirement (OpenAI-Primary/DeepSeek-Fallback
            # sprint): provider=... model=... fallback=... latency_ms=...
            # per request, never api keys/headers/secrets (this file never
            # touches those - only provider/model NAMES and timing).
            log(
                f"request {entry.request_id} finished: provider={provider_used} model={model_used} "
                f"fallback={entry.used_fallback} latency_ms={elapsed:.1f} usage={usage}",
                self.name,
            )
            self.publish(LLMFinished(data={
                **ids, "model": model_used, "provider": provider_used, "execution_time_ms": elapsed,
                "usage": usage, "finish_reason": finish_reason, "fallback": entry.used_fallback,
            }))
            self.publish(AssistantResponse(data={**ids, "text": text, "model": model_used, "provider": provider_used}))

            cost = self._estimate_cost_for(provider_used, model_used, usage.get("prompt_tokens"), usage.get("completion_tokens"))
            self.stats.record(
                provider=provider_used, conversation_id=entry.conversation_id,
                prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
                cost=cost, latency_ms=elapsed, failed=False,
            )
        finally:
            with self._inflight_lock:
                self._inflight.pop(entry.request_id, None)

    # -- non-streaming, with fallback -------------------------------------------

    def _run_non_streaming_with_fallback(
        self, entry: _InFlight, model: Optional[str], messages: List[Dict[str, str]],
        system_prompt: Optional[str], temperature: Optional[float], max_tokens: Optional[int], metadata: Dict[str, Any],
        requested_provider: Optional[str] = None,
    ):
        order = self._priority_order(requested_provider)
        if not order:
            raise ProviderNotConfiguredError("no LLM provider is configured/available")
        last_ex: Optional[ProviderAPIError] = None
        for idx, provider_name in enumerate(order):
            if entry.cancel_event.is_set():
                raise _Cancelled()
            client = self._clients[provider_name]
            entry.provider = provider_name
            # OpenAI-Primary/DeepSeek-Fallback sprint fix: an explicit
            # `model` (from a routing-engine per-request override, e.g.
            # "gpt-5.6-luna") only ever means something to the provider it
            # was resolved FOR - the first one attempted. Passing that same
            # string on to a DIFFERENT vendor after a fallback (e.g.
            # 'openrouter') would send a model id that vendor doesn't
            # recognize (OpenRouter needs "vendor/model" slugs, not a bare
            # OpenAI id) and fail outright instead of actually falling
            # back. Every fallback attempt after the first uses `None` so
            # that provider's OWN configured default model applies.
            attempt_model = model if idx == 0 else None
            try:
                result = client.chat(
                    messages, model=attempt_model, system_prompt=system_prompt, temperature=temperature,
                    max_tokens=max_tokens, metadata=metadata, request_id=entry.request_id,
                )
                return result.text, {"usage": result.usage, "finish_reason": result.finish_reason, "model": result.model}, provider_name
            except ProviderAPIError as ex:
                last_ex = ex
                is_last = idx == len(order) - 1
                if not self._fallback_eligible(ex, is_last):
                    raise
                self._activate_fallback(entry, provider_name, order[idx + 1], ex)
        raise last_ex  # pragma: no cover - loop always returns or raises above

    # -- streaming, with fallback (only before any content is yielded) ----------

    def _run_streaming_with_fallback(
        self, entry: _InFlight, ids: Dict[str, Any], model: Optional[str], messages: List[Dict[str, str]],
        system_prompt: Optional[str], temperature: Optional[float], max_tokens: Optional[int], metadata: Dict[str, Any],
        requested_provider: Optional[str] = None,
    ):
        order = self._priority_order(requested_provider)
        if not order:
            raise ProviderNotConfiguredError("no LLM provider is configured/available")
        last_ex: Optional[ProviderAPIError] = None
        for idx, provider_name in enumerate(order):
            if entry.cancel_event.is_set():
                raise _Cancelled()
            client = self._clients[provider_name]
            entry.provider = provider_name
            # see the identical comment in _run_non_streaming_with_fallback -
            # an explicit model override only applies to the first attempt.
            attempt_model = model if idx == 0 else None
            parts: List[str] = []
            index = 0
            usage: Dict[str, Any] = {}
            finish_reason = None
            any_delta_yielded = False
            try:
                log(f"request {entry.request_id} streaming started via '{provider_name}'", self.name)
                self.publish(LLMStreaming(data={**ids, "model": attempt_model, "provider": provider_name}))
                for chunk in client.stream_chat(
                    messages, model=attempt_model, system_prompt=system_prompt, temperature=temperature,
                    max_tokens=max_tokens, metadata=metadata, cancel_event=entry.cancel_event, request_id=entry.request_id,
                ):
                    if entry.cancel_event.is_set():
                        raise _Cancelled()
                    if chunk.delta:
                        any_delta_yielded = True
                        parts.append(chunk.delta)
                        index += 1
                        self.publish(LLMChunk(data={
                            **ids, "model": model, "provider": provider_name, "delta": chunk.delta,
                            "index": index, "text_so_far": "".join(parts),
                        }))
                    if chunk.finished:
                        usage = {
                            "prompt_tokens": chunk.prompt_tokens, "completion_tokens": chunk.completion_tokens,
                            "total_tokens": chunk.total_tokens,
                        }
                        finish_reason = chunk.finish_reason
                if entry.cancel_event.is_set():
                    raise _Cancelled()
                log(f"request {entry.request_id} streaming finished via '{provider_name}' ({index} chunks)", self.name)
                return "".join(parts), {"usage": usage, "finish_reason": finish_reason, "model": attempt_model}, provider_name
            except _Cancelled:
                raise
            except ProviderAPIError as ex:
                last_ex = ex
                is_last = idx == len(order) - 1
                # already spoke/displayed partial output downstream via
                # LLMChunk - cannot silently retry elsewhere without
                # double-speaking (see module docstring's own note).
                if any_delta_yielded or not self._fallback_eligible(ex, is_last):
                    raise
                self._activate_fallback(entry, provider_name, order[idx + 1], ex)
        raise last_ex  # pragma: no cover

    def _fallback_eligible(self, ex: ProviderAPIError, is_last: bool) -> bool:
        if is_last or not self.manager_config.enable_fallback or not ex.fallback_eligible:
            return False
        if isinstance(ex, ProviderInvalidRequestError) and not self.manager_config.fallback_on_invalid_request:
            return False
        if isinstance(ex, ProviderAuthError) and not self.manager_config.fallback_on_auth_error:
            # OpenAI-Primary/DeepSeek-Fallback sprint: an invalid/expired
            # API key or other auth failure is a configuration problem,
            # not a transient outage - silently switching to DeepSeek
            # would hide it. Surfaces as a normal LLMError instead (see
            # `_run_request`'s except ProviderAPIError branch).
            return False
        return True

    def _activate_fallback(self, entry: _InFlight, from_provider: str, to_provider: str, ex: ProviderAPIError) -> None:
        entry.used_fallback = True
        log(f"request {entry.request_id}: provider '{from_provider}' failed ({ex}) - falling back to '{to_provider}' (fallback_reason={type(ex).__name__})", self.name)
        payload = {
            "request_id": entry.request_id, "from_provider": from_provider, "to_provider": to_provider,
            "reason": str(ex), "error_type": type(ex).__name__, "at": time.time(),
        }
        self._last_fallback = payload
        self.publish(ProviderFallbackActivated(data=payload))

    def _publish_error(self, ids: Dict[str, Any], model: Optional[str], ex: Exception) -> None:
        retryable = getattr(ex, "retryable", False)
        self.publish(LLMError(data={
            **ids, "model": model, "error": str(ex), "error_type": type(ex).__name__, "retryable": retryable,
        }))

    def _estimate_cost_for(self, provider: str, model: Optional[str], prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
        client = self._clients.get(provider)
        if client is None:
            return None
        try:
            info = client.get_model_info(model)
        except Exception:
            return None
        return estimate_cost(prompt_tokens, completion_tokens, info.input_cost_per_1m, info.output_cost_per_1m)

    # -- CancelLLMRequest -----------------------------------------------------

    def _handle_cancel(self, event: Any) -> None:
        request_id = event.get("request_id")
        conversation_id = event.get("conversation_id")
        correlation_id = event.get("correlation_id") or request_id
        provider = None
        with self._inflight_lock:
            entry = self._inflight.get(request_id) if request_id else None
            if entry is not None:
                entry.cancelled = True
                entry.cancel_event.set()
                provider = entry.provider
                conversation_id = conversation_id or entry.conversation_id
                correlation_id = correlation_id or entry.correlation_id
        if provider:
            client = self._clients.get(provider)
            if client is not None:
                try:
                    client.cancel(request_id)
                except Exception:
                    pass
        log(f"request {request_id} cancel requested", self.name)
        self.publish(LLMCancelled(data={
            "request_id": request_id, "conversation_id": conversation_id, "correlation_id": correlation_id,
        }))

    # -- ReloadModel (spec: "Reload configuration" / "Runtime Switching") -------

    def _handle_reload_model(self, event: Any) -> None:
        old_provider = self.manager_config.provider
        new_config = LLMManagerConfig.from_env()

        override_provider = (event.get("provider") or "").strip().lower()
        if override_provider and override_provider in PROVIDER_NAMES:
            new_config.provider = override_provider
            new_config.priority = [override_provider] + [p for p in new_config.priority if p != override_provider]
        override_model = event.get("model")
        if override_model:
            new_config.default_model = override_model
        self.manager_config = new_config

        for name, client in list(self._clients.items()):
            try:
                pcfg = build_provider_config(
                    name, default_timeout_s=self.manager_config.timeout_s,
                    default_max_retries=self.manager_config.max_retries,
                )
                client.reload(pcfg)
                if name in self._client_errors:
                    # config may have JUST gained credentials (a key
                    # rotated in) - try to bring it back up right away
                    # rather than waiting for the next restart.
                    try:
                        client.initialize()
                        self._client_errors.pop(name, None)
                    except Exception as ex:
                        self._client_errors[name] = str(ex)
            except Exception as ex:
                log(f"reload_model: provider '{name}' reload failed: {ex}", self.name)

        log(f"config reloaded (active provider='{self.manager_config.provider}', priority={self.manager_config.priority})", self.name)
        if old_provider != self.manager_config.provider:
            self.publish(ProviderSwitched(data={
                "from_provider": old_provider, "to_provider": self.manager_config.provider, "reason": "config_reload",
            }))

    def switch_provider(self, provider: str) -> bool:
        """Programmatic equivalent of publishing `ReloadModel` with
        `data["provider"]` set - used by the Dashboard's LLM panel
        "switch provider" control (see `dashboard/controls.py`) so it
        doesn't have to construct a synthetic Event Bus event just to
        call this. Returns `False` for an unknown provider name
        (no-op, config unchanged).

        Deliberately does NOT check `provider_configured()` here and
        refuse the switch - a user may legitimately want to set
        `manager_config.provider`/priority to a provider they're ABOUT
        to configure (add the API key, then hit "Reload Configuration"
        without switching again). `dashboard/controls.py::
        switch_llm_provider()` is where the "this isn't actually usable
        yet" warning belongs - it can report success AND a warning in
        the same response, whereas this method's `bool` return can't."""
        provider = (provider or "").strip().lower()
        if provider not in PROVIDER_NAMES:
            return False
        from .events import ReloadModel as _ReloadModel
        self._handle_reload_model(_ReloadModel(data={"provider": provider}))
        return True

    def provider_configured(self, provider: str) -> bool:
        """`True` only if `provider` has a real, initialized, currently-
        usable client - i.e. requests actually routed to it won't
        silently fall through to whichever OTHER provider happens to be
        configured (see `_priority_order()`'s own docstring: an unusable
        provider is filtered out of the priority list entirely, not
        retried-and-failed). Used by the Dashboard's "switch provider"
        control to warn honestly when switching to a provider that
        looks selected but isn't actually going to answer anything -
        see `dashboard/controls.py::switch_llm_provider()`."""
        return provider in self._configured_provider_names()

    # -- ConversationReset ------------------------------------------------------

    def _handle_conversation_reset(self, event: Any) -> None:
        conversation_id = event.get("conversation_id")
        with self._inflight_lock:
            targets = [
                e for e in self._inflight.values()
                if conversation_id is None or e.conversation_id == conversation_id
            ]
            for e in targets:
                e.cancelled = True
                e.cancel_event.set()
        for e in targets:
            client = self._clients.get(e.provider) if e.provider else None
            if client is not None:
                try:
                    client.cancel(e.request_id)
                except Exception:
                    pass
        log(f"conversation_reset: cancelled {len(targets)} in-flight request(s)"
            f"{f' for conversation {conversation_id}' if conversation_id else ' (all conversations)'}", self.name)
        for e in targets:
            self.publish(LLMCancelled(data={
                "request_id": e.request_id, "conversation_id": e.conversation_id, "correlation_id": e.correlation_id,
                "reason": "conversation_reset",
            }))

    # -- direct (non-event-bus) convenience call --------------------------------

    def chat_once(
        self, messages: List[Dict[str, str]], *, model: Optional[str] = None,
        system_prompt: Optional[str] = None, temperature: Optional[float] = None,
        max_tokens: Optional[int] = None, metadata: Optional[Dict[str, Any]] = None, use_fallback: bool = True,
        provider: Optional[str] = None,
    ) -> ChatResult:
        """Blocking, synchronous, provider-priority-aware `chat()` for
        callers OUTSIDE the Event Bus (`.client` legacy shim - see
        module docstring). NOT used by `_handle_need_llm_response()`'s
        own path (that one tracks `entry.provider`/publishes per-
        provider events directly against each client).

        `provider` (Efficient LLM Classifier sprint, additive/optional -
        every pre-existing caller passes nothing and gets byte-identical
        behavior): pins this ONE call to try `provider` first via the
        exact same per-request `_priority_order(requested_provider=...)`
        override `_handle_need_llm_response()` already uses for
        `NeedLLMResponse.data["provider"]` - see that method's own
        docstring. Never mutates `self.manager_config` (the GLOBAL active
        provider), so a classifier call pinned to "openai" can never
        switch what the rest of Luno's replies are answered by; if
        `provider` isn't usable, `_priority_order()` silently falls back
        to the normal configured order, same fail-open behavior as the
        NeedLLMResponse path."""
        order = self._priority_order(provider)
        if not order:
            raise ProviderNotConfiguredError("no LLM provider is configured/available")
        if not use_fallback:
            order = order[:1]
        last_ex: Optional[ProviderAPIError] = None
        for idx, provider_name in enumerate(order):
            client = self._clients[provider_name]
            try:
                return client.chat(
                    messages, model=model, system_prompt=system_prompt, temperature=temperature,
                    max_tokens=max_tokens, metadata=metadata,
                )
            except ProviderAPIError as ex:
                last_ex = ex
                if not self._fallback_eligible(ex, idx == len(order) - 1):
                    raise
        raise last_ex  # pragma: no cover

    # -- backward-compat surface (see module docstring) --------------------------

    @property
    def default_model(self) -> Optional[str]:
        if self.manager_config.default_model:
            return self.manager_config.default_model
        client = self._clients.get(self.manager_config.provider)
        config = getattr(client, "config", None)
        return config.model if config is not None else None

    @property
    def is_mock_active_provider(self) -> bool:
        from .llm.mock import MockProviderClient
        client = self._clients.get(self.manager_config.provider)
        if client is None or self.manager_config.provider in self._client_errors:
            return True
        return isinstance(client, MockProviderClient)

    @property
    def client(self) -> _LegacyClientShim:
        return _LegacyClientShim(self)

    # -- model catalog (spec: "Model Selection") ---------------------------------

    def list_all_models(self) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        for name in self._configured_provider_names():
            client = self._clients.get(name)
            if client is None:
                continue
            try:
                result[name] = [m.to_dict() for m in client.list_models()]
            except Exception as ex:
                log(f"list_all_models: provider '{name}' raised: {ex}", self.name)
                result[name] = []
        return result

    def capabilities_for(self, provider: Optional[str] = None, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
        client = self._clients.get(provider or self.manager_config.provider)
        if client is None:
            return None
        try:
            return client.capabilities(model).to_dict()
        except Exception:
            return None

    # -- status -----------------------------------------------------------------

    def _extra_status(self) -> Dict[str, Any]:
        with self._inflight_lock:
            inflight_count = len(self._inflight)
        return {
            "active_provider": self.manager_config.provider,
            "default_model": self.default_model,
            "priority": list(self.manager_config.priority),
            "enable_fallback": self.manager_config.enable_fallback,
            "enable_streaming": self.manager_config.enable_streaming,
            "inflight_requests": inflight_count,
            "configured_providers": self._configured_provider_names(),
            "unconfigured_providers": dict(self._client_errors),
            "health": self.provider_health_all(),
            "stats": self.stats.to_dict(),
            "last_fallback": dict(self._last_fallback) if self._last_fallback else None,
        }
