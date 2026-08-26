"""
llm_classifier.py
===================

Efficient LLM Classifier sprint - a small, OPTIONAL GPT-5.4-nano fallback
classifier for utterances `intent_classifier.classify_intent()` couldn't
place at all (fell back to GENERAL_QUESTION/GENERAL_CHAT - see
`decision_engine.py::decide()`'s own "ambiguous gate" for exactly when
this gets called; NEVER on a normal, already-confidently-classified turn).

Hard boundaries (spec's own words, enforced here structurally):
    - Classifies ONLY. Never executes a tool, never controls Home
      Assistant/browser, never answers the user, never performs
      reasoning, never replaces the main conversational LLM or
      `DecisionEngine` itself - `classify_intent_llm()` returns a
      `ClassifierResult` or `None`, nothing else, no side effects beyond
      one read-only API call.
    - The `intent` it returns is always one of `luno.routing.models.Intent`
      - the EXISTING 15-category taxonomy, never a second incompatible
        one, never an arbitrary tool/action name.
    - Never proof of tool success - see `decision_engine.py`'s own
      docstring; this module doesn't even know what a `ToolResult` is.
    - Fails closed on ANYTHING going wrong (timeout, malformed JSON, a
      field failing validation, a network/API error) - returns `None`,
      exactly like `PlannerBridgeModule._classify_device_intent()`'s own
      "always fails closed, silently, so a classification hiccup
      degrades to today's plain-chat behavior" precedent. Never raises.

`chat_fn` is dependency-injected (never imports/constructs a provider
client itself) - callers pass the exact same
`LLMManagerAdapter.client.chat_completion` callable
`PlannerBridgeModule._classify_device_intent()` already uses (see
`luno/bootstrap/adapters.py::register_intent_classifier()`), just with
`provider="openai"` and a `response_format` JSON schema attached via
`metadata` (both additive, backward-compatible extensions - see
`llm_manager.py::chat_once()`/`openai_provider.py::_extra_payload_fields()`
docstrings for the plumbing this reuses).

Timeout: `chat_fn` itself has no per-call timeout parameter (the
provider client's HTTP timeout is a fixed, much longer, per-CLIENT
config value meant for real conversational replies - see
`luno/adapters/llm/base.py`). `CLASSIFIER_TIMEOUT_MS` is enforced HERE
instead, via a dedicated small worker pool + `Future.result(timeout=...)`
- the exact same "honest limitation: Python can't force-kill a thread,
so a timed-out call's result is simply discarded" pattern
`luno.tool_manager.manager.ToolManager` already documents and uses for
its own per-call timeout. Runs off whatever thread the caller is
already on (`PlannerBridgeModule._handle_utterance`'s own per-turn
background thread, never the Event Bus pump thread - see spec section
13), so blocking that ONE caller thread while this pool does the real
work is safe, matching `_classify_device_intent()`'s own accepted
trade-off.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from .models import Intent

if TYPE_CHECKING:
    from .config import RoutingConfig

_VALID_INTENTS = tuple(i.value for i in Intent)

#: OpenAI Structured Outputs request-body field - see
#: `openai_provider.py::_extra_payload_fields()`'s own docstring for how
#: this reaches the actual HTTP request. `strict: true` makes the API
#: itself reject any response that doesn't conform (extra defense on top
#: of this module's own manual validation below, not a replacement for
#: it - a non-OpenAI fallback provider would silently ignore this field
#: entirely, per that method's docstring, so manual validation stays the
#: only thing this module can actually rely on).
_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "intent_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": list(_VALID_INTENTS)},
                "confidence": {"type": "number"},
                "needs_confirmation": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["intent", "confidence", "needs_confirmation", "reason"],
            "additionalProperties": False,
        },
    },
}

_SYSTEM_PROMPT = (
    "You are a routing classifier for a home-automation voice assistant named Luno. "
    "You NEVER execute anything, never control any device, never browse the web, "
    "never answer the user directly, and never perform multi-step reasoning yourself - "
    "you ONLY classify which category the message most likely belongs to, so an "
    "existing router can decide where to send it. This is your entire job.\n\n"
    "Valid categories (choose exactly one):\n"
    + "\n".join(f"- {v}" for v in _VALID_INTENTS)
    + "\n\nReturn `confidence` as your own honest certainty from 0.0 (pure guess) to "
    "1.0 (certain) - do not default to a high number out of politeness. Set "
    "`needs_confirmation` to true only when you think Luno should double-check with "
    "the user before acting on this classification rather than acting immediately. "
    "Keep `reason` to one short sentence explaining the choice."
)

#: Small, lazily-created worker pool purely for timeout-bounding the
#: synchronous `chat_fn` call (see module docstring's "Timeout" section) -
#: classifier calls are infrequent (gated behind the ambiguous-only path
#: in `decision_engine.py`), so a couple of workers is plenty; never
#: shared with `ToolManager`'s own pools (a different package's concern).
_pool_lock = threading.Lock()
_pool: Optional[ThreadPoolExecutor] = None


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="luno-intent-classifier")
        return _pool


@dataclass(frozen=True)
class ClassifierResult:
    intent: Intent
    confidence: float
    needs_confirmation_hint: bool
    reason: str
    latency_ms: float


class ClassifierCache:
    """Tiny in-memory TTL cache, keyed on normalized `text` alone - safe
    because the classifier call itself never receives conversation
    history or any other session-specific context (see module docstring/
    spec section 8's "context minimization"), so identical text always
    means an identical classification request regardless of which
    conversation asked it. `ttl_s <= 0` disables caching entirely (every
    call is a genuine miss) - see spec section 9's "if caching adds more
    complexity than value, document why" get-out clause: a TTL of 0 is
    that documented opt-out, reachable via `CLASSIFIER_CACHE_TTL_SECONDS=0`
    without touching code. Bounded (`_MAX_ENTRIES`) so a long-running
    process fielding many distinct ambiguous utterances can't grow this
    dict unboundedly even with a long TTL - same defensive-bound
    convention `PlannerBridgeModule._pending_turns_max`/
    `_last_device_target_max` already use."""

    _MAX_ENTRIES = 500

    def __init__(self, ttl_s: float) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._store: Dict[str, tuple] = {}  # key -> (ClassifierResult, expires_at)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[ClassifierResult]:
        if self.ttl_s <= 0:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key: str, value: ClassifierResult) -> None:
        if self.ttl_s <= 0:
            return
        with self._lock:
            if len(self._store) >= self._MAX_ENTRIES:
                # Cheap defensive eviction (not true LRU) - drop an
                # arbitrary batch rather than tracking access order for a
                # cache this small and short-lived; correctness (never
                # serving a stale/wrong entry) never depends on WHICH
                # entries survive, only on the TTL check in `get()` above.
                for k in list(self._store.keys())[:100]:
                    self._store.pop(k, None)
            self._store[key] = (value, time.monotonic() + self.ttl_s)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "entries": len(self._store)}


def classify_intent_llm(
    text: str,
    chat_fn: Optional[Callable[..., Any]],
    config: "RoutingConfig",
    *,
    cache: Optional[ClassifierCache] = None,
) -> Optional[ClassifierResult]:
    """Returns a `ClassifierResult`, or `None` for absolutely anything
    that isn't a clean, validated success (see module docstring's "fails
    closed" rule) - `chat_fn is None` (classifier not wired - same
    opt-in-by-construction convention as `device_intent_client`),
    `config.classifier_enabled` being irrelevant here (the CALLER
    - `decision_engine.py` - is the one place that checks that flag,
    this function has no opinion on whether it should have been called),
    empty text, a timed-out/raised `chat_fn` call, unparseable JSON, or
    any field failing validation against the real `Intent` enum / a
    0.0-1.0 confidence range."""
    if chat_fn is None or not text or not text.strip():
        return None

    truncated = text.strip()[: max(1, config.classifier_max_input_chars)]
    cache_key = truncated.lower()
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    t0 = time.monotonic()
    future = _get_pool().submit(
        chat_fn,
        model=config.classifier_model,
        messages=[{"role": "user", "content": truncated}],
        system_prompt=_SYSTEM_PROMPT,
        temperature=0.0,
        max_tokens=200,
        provider="openai",
        metadata={"response_format": _RESPONSE_FORMAT},
    )
    try:
        response = future.result(timeout=max(0.05, config.classifier_timeout_ms / 1000.0))
    except _FuturesTimeoutError:
        return None  # the underlying call keeps running in the background until it
        # naturally finishes; its result is simply discarded - same honest
        # limitation ToolManager's own per-call timeout documents.
    except Exception:
        return None  # network error, auth error, rate limit, anything - fail closed.
    latency_ms = (time.monotonic() - t0) * 1000.0

    raw = (getattr(response, "text", None) or "").strip()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None

    intent_value = parsed.get("intent")
    if not isinstance(intent_value, str) or intent_value not in _VALID_INTENTS:
        return None
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None

    result = ClassifierResult(
        intent=Intent(intent_value),
        confidence=confidence,
        needs_confirmation_hint=bool(parsed.get("needs_confirmation", False)),
        reason=str(parsed.get("reason") or "")[:200],
        latency_ms=latency_ms,
    )
    if cache is not None:
        cache.set(cache_key, result)
    return result
