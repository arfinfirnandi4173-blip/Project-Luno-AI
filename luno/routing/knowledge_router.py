"""
knowledge_router.py
=====================

`KnowledgeRouter` - spec's "Knowledge Routing": before ever going to the
LLM (let alone the internet), check whether the answer is already known
somewhere cheap and local, in priority order:

    World Model -> Long-Term Memory -> Vision Memory -> Planner State ->
    Tool State -> Home Assistant State -> (only then) Internet Search ->
    (only then) LLM

Never calls anything itself - every source is either data already
computed by the ONE real caller (`PlannerBridgeModule._handle_utterance`,
which already runs `memory_retriever.retrieve_memories()` and owns
`self.world_model`/`self.planner`/the just-executed `plan.tasks` before
this routing decision is even made - see that method) or a read-only
callable it hands in. This module never reads Home Assistant, the
Planner, Vision Memory, or the World Model directly - it only ever reads
what was ALREADY fetched for this turn, so it can never trigger a
duplicate query and can never see something the rest of the system
hasn't already verified.

"World Model" and "Home Assistant State" are deliberately backed by the
SAME accessor in the real wiring (`main_runtime_demo.py`) - per
`luno/world_model.py`'s own docstring, the World Model already IS "the
Single Source of Truth" for live Home Assistant device state (push-
synced from the same `device_state_changed` events/verified `ToolResult`
HA itself produces, "no polling, anywhere, ever"). Querying HA again
here would be a redundant, policy-violating duplicate read of a system
this package must never bypass. Both are kept as distinct
`KnowledgeSource` values purely so a future second live-state source
(that ISN'T just the World Model) has somewhere to plug in without a
schema change - see `route()`'s `ha_state_hit` parameter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .models import KnowledgeSource

_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "at", "for",
    "and", "or", "do", "does", "did", "what", "who", "when", "where", "why", "how",
    "yang", "di", "ke", "dari", "dan", "atau", "apa", "apakah", "siapa", "kapan",
    "dimana", "kenapa", "mengapa", "bagaimana", "itu", "ini", "saya", "aku", "kamu",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _significant_tokens(text: str) -> List[str]:
    return [t for t in _WORD_RE.findall((text or "").lower()) if len(t) >= 3 and t not in _STOPWORDS]


@dataclass
class KnowledgeSourceResult:
    source: KnowledgeSource
    hit: bool
    detail: Optional[str] = None
    #: every source actually checked, in order, for logging/debugging -
    #: `[(source, hit), ...]`.
    checked: Optional[List[Any]] = None


class KnowledgeRouter:
    """Stateless (safe to share across requests/threads) - every call to
    `route()` is handed a fresh snapshot of this turn's already-fetched
    data, nothing is cached or retained between calls."""

    def route(
        self,
        *,
        text: str,
        world_model_entities: Optional[Dict[str, Any]] = None,
        relevant_memories: Optional[List[Any]] = None,
        tool_state_hit: bool = False,
        tool_state_detail: Optional[str] = None,
        ha_state_hit: bool = False,
    ) -> KnowledgeSourceResult:
        checked: List[Any] = []
        tokens = set(_significant_tokens(text))

        # 1) World Model - does any tracked entity's id/name overlap with
        #    a significant word in the utterance? (heuristic, NOT the
        #    same precise device-name resolution `RealHomeAssistantHandler`
        #    does for actually issuing a command - this is only ever used
        #    to decide "do we already know something relevant", never to
        #    control a device.)
        wm_hit, wm_detail = self._match_world_model(tokens, world_model_entities or {})
        checked.append((KnowledgeSource.WORLD_MODEL.value, wm_hit))
        if wm_hit:
            return KnowledgeSourceResult(KnowledgeSource.WORLD_MODEL, True, wm_detail, checked)

        # 2) Long-Term Memory (explicit + retrieved facts already surfaced
        #    by MemoryRetriever this turn - source "long_term_memory").
        ltm_hit, ltm_detail = self._match_memory_source(relevant_memories, {"long_term_memory"})
        checked.append((KnowledgeSource.LONG_TERM_MEMORY.value, ltm_hit))
        if ltm_hit:
            return KnowledgeSourceResult(KnowledgeSource.LONG_TERM_MEMORY, True, ltm_detail, checked)

        # 3) Vision Memory (objects/humans/events sources).
        vm_hit, vm_detail = self._match_memory_source(
            relevant_memories, {"vision_objects", "vision_human", "vision_events"},
        )
        checked.append((KnowledgeSource.VISION_MEMORY.value, vm_hit))
        if vm_hit:
            return KnowledgeSourceResult(KnowledgeSource.VISION_MEMORY, True, vm_detail, checked)

        # 4) Planner State (what the Planner itself last did/knows).
        ps_hit, ps_detail = self._match_memory_source(relevant_memories, {"planner_state"})
        checked.append((KnowledgeSource.PLANNER_STATE.value, ps_hit))
        if ps_hit:
            return KnowledgeSourceResult(KnowledgeSource.PLANNER_STATE, True, ps_detail, checked)

        # 5) Tool State - a real tool already ran THIS turn with a
        #    verified result (see `_handle_utterance`'s `real_tasks`).
        checked.append((KnowledgeSource.TOOL_STATE.value, tool_state_hit))
        if tool_state_hit:
            return KnowledgeSourceResult(KnowledgeSource.TOOL_STATE, True, tool_state_detail, checked)

        # 6) Home Assistant State - see module docstring: in the real
        #    wiring this is the same signal as World Model, kept separate
        #    only for future extensibility/testability.
        checked.append((KnowledgeSource.HOME_ASSISTANT_STATE.value, ha_state_hit))
        if ha_state_hit:
            return KnowledgeSourceResult(KnowledgeSource.HOME_ASSISTANT_STATE, True, None, checked)

        return KnowledgeSourceResult(KnowledgeSource.NONE, False, None, checked)

    @staticmethod
    def _match_world_model(tokens: set, entities: Dict[str, Any]) -> "tuple[bool, Optional[str]]":
        if not tokens or not entities:
            return False, None
        for entity_id, state in entities.items():
            entity_tokens = set(_significant_tokens(str(entity_id).replace(".", " ").replace("_", " ")))
            if tokens & entity_tokens:
                return True, f"{entity_id}={state}"
        return False, None

    @staticmethod
    def _match_memory_source(relevant_memories: Optional[List[Any]], source_names: set) -> "tuple[bool, Optional[str]]":
        if not relevant_memories:
            return False, None
        for mem in relevant_memories:
            source = getattr(mem, "source", None)
            if source in source_names:
                return True, getattr(mem, "text", None)
        return False, None
