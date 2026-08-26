"""
Smart Memory Injection (Retrieval-Based)
==========================================

Sprint 5's addition: a retrieval layer that sits BETWEEN the existing
Vision Memory / Context Builder / OpenRouter Adapter and decides which
memories, if any, are actually relevant to the user's current message -
so the LLM prompt only ever carries a handful of relevant facts instead
of Vision Memory's entire database.

Nothing here modifies `vision_memory`, `core.context_builder`, or
`adapters.openrouter` - this is a new, independent, standalone package
(same convention as every other subsystem: `wake_session`, `barge_in`,
`text_normalizer`) that reads from those existing systems only through
injected, zero/one-arg provider callables (see `sources.py`), the exact
same "hand in a snapshot, not a live dependency" shape `ContextBuilder`
and `PlannerContext`/`Handlers` already use.

    models.py     - RelevantMemory / QueryAnalysis / MemoryRetrievalConfig
    query.py      - pure keyword-based relevance detection (no I/O)
    sources.py    - built-in MemorySource factories (Vision Memory,
                    Long-Term Memory, Planner State, Tool Execution)
    retriever.py  - MemoryRetriever: registry + ranking + limits +
                    staleness + debug logging
    prompt.py     - build_memory_prompt_block(): renders the final
                    "Relevant Memory:" block for the system prompt

Quick start (standalone)
-------------------------
    from luno.memory_retrieval import (
        MemoryRetriever, MemoryRetrievalConfig,
        make_vision_object_source, make_vision_human_source,
        build_memory_prompt_block,
    )
    from luno import vision_memory as vm

    retriever = MemoryRetriever(MemoryRetrievalConfig.from_env())
    retriever.register_source("vision_objects", make_vision_object_source(vm.get_world_state))
    retriever.register_source("vision_human", make_vision_human_source(vm.get_world_state))

    memories = retriever.retrieve_memories("where is my cup?")
    block = build_memory_prompt_block(memories)
    # block == "Relevant Memory:\n- Cup last seen on the desk. Observed 3 minutes ago."

See `main_runtime_demo.py` for the full, real wiring inside
`PlannerBridgeModule._handle_utterance()`.
"""

from .models import MemoryRetrievalConfig, QueryAnalysis, RelevantMemory
from .prompt import build_memory_prompt_block
from .query import analyze_query, token_overlap
from .retriever import MemoryRetriever
from .sources import (
    MemorySource,
    make_long_term_memory_source,
    make_planner_state_source,
    make_tool_execution_source,
    make_vision_event_source,
    make_vision_human_source,
    make_vision_object_source,
)

__all__ = [
    "RelevantMemory", "QueryAnalysis", "MemoryRetrievalConfig",
    "analyze_query", "token_overlap",
    "MemorySource",
    "make_vision_object_source", "make_vision_human_source", "make_vision_event_source",
    "make_long_term_memory_source", "make_planner_state_source", "make_tool_execution_source",
    "MemoryRetriever",
    "build_memory_prompt_block",
]
