"""
stats.py
========

`LLMStats` - the spec's "Cost Tracking"/"Statistics" section: prompt/
completion/total tokens and estimated cost, rolled up per provider, per
conversation, and per day, plus request counts and a rolling average
latency per provider - exactly what `LLMManagerAdapter._extra_status()`
and the Dashboard's `/api/llm` endpoint (see `dashboard/collectors.py`)
surface. Pure bookkeeping - never touches the network, never decides
anything; `LLMManagerAdapter` calls `record()` once per finished
request (success or failure) and reads `to_dict()` for status/Dashboard.

Cost estimation is deliberately best-effort: `ModelInfo.input_cost_per_1m`/
`output_cost_per_1m` come from each provider module's small, hand-
maintained catalog (see e.g. `openai_provider.py`'s `_MODEL_CATALOG`) -
accurate for the models actually listed there, `None` ("cost unknown",
never a silent guess) for anything else, including every local model
(genuinely free/uncounted) and every OpenRouter/custom slug this
package hasn't been told the price of.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class _Bucket:
    """One rollup unit (a provider, a conversation, or a day) - fields
    match 1:1 with the spec's "Track: Prompt Tokens / Completion Tokens
    / Total Tokens / Estimated Cost" list."""
    requests: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float = 0.0
    cost_is_estimate_complete: bool = True  # False once ANY request in this bucket had unknown pricing

    def add(self, prompt_tokens: Optional[int], completion_tokens: Optional[int], cost: Optional[float], failed: bool) -> None:
        self.requests += 1
        if failed:
            self.failures += 1
        self.prompt_tokens += prompt_tokens or 0
        self.completion_tokens += completion_tokens or 0
        self.total_tokens += (prompt_tokens or 0) + (completion_tokens or 0)
        if cost is None:
            self.cost_is_estimate_complete = False
        else:
            self.estimated_cost += cost

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests, "failures": self.failures,
            "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost, 6),
            "cost_is_estimate_complete": self.cost_is_estimate_complete,
        }


def estimate_cost(prompt_tokens: Optional[int], completion_tokens: Optional[int], input_cost_per_1m: Optional[float], output_cost_per_1m: Optional[float]) -> Optional[float]:
    """`None` whenever pricing for this model is unknown - see module
    docstring. Never guesses."""
    if input_cost_per_1m is None or output_cost_per_1m is None:
        return None
    return ((prompt_tokens or 0) / 1_000_000.0) * input_cost_per_1m + ((completion_tokens or 0) / 1_000_000.0) * output_cost_per_1m


class LLMStats:
    #: how many recent per-request latency samples to keep, per provider,
    #: for `avg_latency_ms`/`p95-ish` reporting without unbounded memory
    #: growth over a long-running Runtime (spec's "Stress test (500
    #: requests)" implies this needs to stay cheap at that scale too).
    _LATENCY_WINDOW = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_provider: Dict[str, _Bucket] = defaultdict(_Bucket)
        self._by_conversation: Dict[str, _Bucket] = defaultdict(_Bucket)
        self._by_day: Dict[str, Dict[str, _Bucket]] = defaultdict(lambda: defaultdict(_Bucket))
        self._latencies: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=self._LATENCY_WINDOW))
        self._last_request_at: Dict[str, float] = {}

    def record(
        self, *, provider: str, conversation_id: Optional[str], prompt_tokens: Optional[int],
        completion_tokens: Optional[int], cost: Optional[float], latency_ms: Optional[float], failed: bool = False,
    ) -> None:
        with self._lock:
            self._by_provider[provider].add(prompt_tokens, completion_tokens, cost, failed)
            if conversation_id:
                self._by_conversation[conversation_id].add(prompt_tokens, completion_tokens, cost, failed)
            self._by_day[_today()][provider].add(prompt_tokens, completion_tokens, cost, failed)
            if latency_ms is not None:
                self._latencies[provider].append(latency_ms)
            self._last_request_at[provider] = time.time()

    def avg_latency_ms(self, provider: str) -> Optional[float]:
        with self._lock:
            samples = self._latencies.get(provider)
            if not samples:
                return None
            return sum(samples) / len(samples)

    def requests_for(self, provider: str) -> int:
        with self._lock:
            return self._by_provider[provider].requests

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "by_provider": {k: v.to_dict() for k, v in self._by_provider.items()},
                "by_conversation": {k: v.to_dict() for k, v in self._by_conversation.items()},
                "by_day": {day: {p: b.to_dict() for p, b in providers.items()} for day, providers in self._by_day.items()},
                "avg_latency_ms": {p: (sum(d) / len(d) if d else None) for p, d in self._latencies.items()},
                "last_request_at": dict(self._last_request_at),
            }
