"""
stats.py
=========

`RoutingStats` - decision-level telemetry for the Dashboard's "Decision
Engine" panel: how many turns were routed where, and why, over time.
Deliberately does NOT duplicate token/dollar cost accounting -
`LLMManagerAdapter.stats` (`luno.adapters.llm.stats.LLMStats`) already
does that correctly, keyed by the REAL provider that answered
(`openrouter`/`openai`/...), and every routed request still flows
through that exact same accounting path (a `provider` override on
`NeedLLMResponse` doesn't change how `_run_request()` records usage -
see `llm_manager.py`). Re-deriving cost here would risk silently
drifting from that one real source of truth. `dashboard/collectors.py`
joins this module's decision counts with `LLMManagerAdapter`'s real
cost/token stats for the spec's "daily/conversation/provider/token"
cost view - see `collect_routing_status()`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

if TYPE_CHECKING:
    from .models import RoutingDecision


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class RoutingStats:
    def __init__(self, recent_limit: int = 200) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._by_provider_alias: Dict[str, int] = {}
        self._by_intent: Dict[str, int] = {}
        self._by_day: Dict[str, int] = {}
        self._by_conversation: Dict[str, int] = {}
        self._internet_searches = 0
        self._knowledge_shortcuts = 0
        self._affinity_applied = 0
        # Efficient LLM Classifier sprint - dashboard/efficiency-test
        # telemetry (spec sections 11/17). `_classifier_bypassed` is every
        # decision that DIDN'T invoke the classifier (the overwhelming
        # common case - a deterministic rule matched, or it was
        # ambiguous but the classifier is disabled/not wired) - the
        # efficiency test asserts this stays near-total for clearly-
        # phrased commands.
        self._classifier_calls = 0
        self._classifier_confirmations = 0
        self._classifier_latency_total_ms = 0.0
        self._classifier_bypassed = 0
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=recent_limit)

    def record(self, decision: "RoutingDecision") -> None:
        with self._lock:
            self._total += 1
            self._by_provider_alias[decision.provider_alias] = self._by_provider_alias.get(decision.provider_alias, 0) + 1
            self._by_intent[decision.primary_intent.value] = self._by_intent.get(decision.primary_intent.value, 0) + 1
            day = _day_key(decision.timestamp)
            self._by_day[day] = self._by_day.get(day, 0) + 1
            if decision.conversation_id:
                self._by_conversation[decision.conversation_id] = self._by_conversation.get(decision.conversation_id, 0) + 1
            if decision.needs_internet:
                self._internet_searches += 1
            if decision.knowledge_hit and not decision.needs_internet:
                self._knowledge_shortcuts += 1
            if decision.affinity_applied:
                self._affinity_applied += 1
            if decision.used_classifier:
                self._classifier_calls += 1
                if decision.classifier_latency_ms is not None:
                    self._classifier_latency_total_ms += decision.classifier_latency_ms
                if decision.needs_confirmation:
                    self._classifier_confirmations += 1
            else:
                self._classifier_bypassed += 1
            self._recent.append(decision.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            avg_classifier_latency_ms = (
                (self._classifier_latency_total_ms / self._classifier_calls) if self._classifier_calls else 0.0
            )
            return {
                "total_decisions": self._total,
                "by_provider_alias": dict(self._by_provider_alias),
                "by_intent": dict(self._by_intent),
                "by_day": dict(self._by_day),
                "by_conversation": dict(self._by_conversation),
                "internet_searches": self._internet_searches,
                "knowledge_shortcuts": self._knowledge_shortcuts,
                "affinity_applied": self._affinity_applied,
                "classifier_calls": self._classifier_calls,
                "classifier_bypassed": self._classifier_bypassed,
                "classifier_confirmations": self._classifier_confirmations,
                "classifier_avg_latency_ms": round(avg_classifier_latency_ms, 2),
                "recent": list(self._recent),
            }
