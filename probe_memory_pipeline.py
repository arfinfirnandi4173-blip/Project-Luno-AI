"""
Read-only production-path reproduction probe for the MEMORY RETRIEVAL &
DECISION QUALITY sprint (Phase 1-2). Instruments the REAL pipeline via
monkeypatches on the actual module functions (never a second
implementation) and drives turns through the REAL RuntimeDemoConsole /
PlannerBridgeModule event path, exactly like tests/test_memory_continuity.py's
own `_new_console`/`_run_turn_and_capture` helpers (reused verbatim below).

Captures, per turn, as close to the brief's 20 data points as the real
pipeline actually exposes them.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
from typing import Callable

ROOT = "/sessions/lucid-dazzling-darwin/mnt/Luno Evo"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tempfile

import luno.config as luno_config
import luno.memory as memory
import luno.memory_context as memory_context
from luno.memory_retrieval.retriever import MemoryRetriever

# ─────────────────────────────────────────────
# Persistent-state safety (Phase 12): mirrors tests/conftest.py's own
# `isolate_persistent_state` fixture EXACTLY (same attribute list) - this
# is a raw script, not a pytest test, so that autouse fixture never runs
# here unless we replicate it ourselves. Every writer-capable store is
# redirected to a throwaway temp-dir path BEFORE any console/module that
# might read or write it is constructed. Vinn's real config/*.json files
# are never touched by this run.
# ─────────────────────────────────────────────
_ISOLATION_DIR = tempfile.mkdtemp(prefix="luno_probe_isolated_state_")
_WRITABLE_STATE_ATTRS = (
    "RELATIONSHIP_STATE_FILE",
    "EPISODIC_MEMORY_FILE",
    "LONG_TERM_MEMORY_FILE",
    "SESSION_SUMMARIES_FILE",
    "HABIT_MEMORY_FILE",
    "REMINDERS_FILE",
    "VERIFIED_FACTS_FILE",
    "RESPONSE_DEPTH_PREFERENCE_FILE",
)
for _attr in _WRITABLE_STATE_ATTRS:
    if hasattr(luno_config, _attr):
        setattr(luno_config, _attr, os.path.join(_ISOLATION_DIR, f"{_attr}.json"))
print(f"[isolation] persistent state redirected to {_ISOLATION_DIR}")

# ─────────────────────────────────────────────
# Instrumentation state (reset per turn)
# ─────────────────────────────────────────────
CURRENT = {}
TURN_LOG = []


def _reset_current():
    CURRENT.clear()
    CURRENT.update({
        "intent": None,
        "reference_type": None,
        "is_short_followup": None,
        "topic_history_before": None,
        "topic_candidates_selected": None,
        "expanded_retrieval_text": None,
        "retriever_raw_candidates": None,
        "retriever_post_recency": None,
        "retriever_post_dedup": None,
        "retriever_post_limits": None,
        "context_items_pre_dedup": None,
        "context_items_pre_budget": None,
        "context_items_post_budget": None,
        "rendered_block": None,
        "assemble_funnel": None,
    })


_reset_current()

# ---- luno.memory ----
_orig_classify_intent = memory.classify_query_intent
def _wrap_classify_intent(text):
    r = _orig_classify_intent(text)
    CURRENT["intent"] = r
    return r
memory.classify_query_intent = _wrap_classify_intent

_orig_classify_ref = memory.classify_reference_type
def _wrap_classify_ref(text):
    r = _orig_classify_ref(text)
    CURRENT["reference_type"] = r
    return r
memory.classify_reference_type = _wrap_classify_ref

# ---- luno.memory_context ----
_orig_select_topic = memory_context.select_topic_candidates
def _wrap_select_topic(history, text, is_short_followup=False):
    CURRENT["topic_history_before"] = [
        {"terms": sorted(e.terms), "turns_since_active": e.turns_since_active, "stale": e.is_stale}
        for e in (history or [])
    ]
    CURRENT["is_short_followup"] = is_short_followup
    r = _orig_select_topic(history, text, is_short_followup)
    CURRENT["topic_candidates_selected"] = [
        {"terms": sorted(e.terms), "turns_since_active": e.turns_since_active} for e in r
    ]
    return r
memory_context.select_topic_candidates = _wrap_select_topic

_orig_build_expanded = memory_context.build_expanded_retrieval_text_from_history
def _wrap_build_expanded(text, entries):
    r = _orig_build_expanded(text, entries)
    CURRENT["expanded_retrieval_text"] = r
    return r
memory_context.build_expanded_retrieval_text_from_history = _wrap_build_expanded

# ---- MemoryRetriever (class-level patch, affects every instance) ----
_orig_retrieve_memories = MemoryRetriever.retrieve_memories
def _wrap_retrieve_memories(self, user_text):
    r = _orig_retrieve_memories(self, user_text)
    CURRENT["retriever_raw_candidates"] = [
        {"source": rm.source, "text": rm.text, "score": round(rm.score, 4), "stale": rm.stale}
        for rm in r
    ]
    return r
MemoryRetriever.retrieve_memories = _wrap_retrieve_memories

_orig_apply_recency = MemoryRetriever._apply_recency_and_staleness
def _wrap_apply_recency(self, candidates):
    r = _orig_apply_recency(self, candidates)
    CURRENT["retriever_post_recency"] = [
        {"source": rm.source, "text": rm.text[:60], "score": round(rm.score, 4)} for rm in r
    ]
    return r
MemoryRetriever._apply_recency_and_staleness = _wrap_apply_recency

_orig_dedup = MemoryRetriever._deduplicate
def _wrap_dedup(self, candidates):
    r = _orig_dedup(self, candidates)
    CURRENT["retriever_post_dedup"] = len(r)
    return r
MemoryRetriever._deduplicate = _wrap_dedup

_orig_limits = MemoryRetriever._apply_limits
def _wrap_limits(self, candidates):
    r = _orig_limits(self, candidates)
    CURRENT["retriever_post_limits"] = [
        {"source": rm.source, "text": rm.text[:60], "score": round(rm.score, 4)} for rm in r
    ]
    if len(r) < len(candidates):
        CURRENT["retriever_limits_dropped"] = [
            {"source": rm.source, "text": rm.text[:60], "score": round(rm.score, 4)}
            for rm in candidates if rm not in r
        ]
    return r
MemoryRetriever._apply_limits = _wrap_limits

_orig_apply_budget = memory_context._apply_budget
def _wrap_apply_budget(items, config):
    CURRENT["context_items_pre_budget"] = [
        {"source": it.source, "text": it.text[:80], "rank_key": it._rank_key()} for it in items
    ]
    r = _orig_apply_budget(items, config)
    CURRENT["context_items_post_budget"] = [
        {"source": it.source, "text": it.text[:80]} for it in r
    ]
    return r
memory_context._apply_budget = _wrap_apply_budget

_orig_dedup_items = memory_context.deduplicate_context_items
def _wrap_dedup_items(items):
    CURRENT["context_items_pre_dedup"] = [
        {"source": it.source, "text": it.text[:80]} for it in items
    ]
    return _orig_dedup_items(items)
memory_context.deduplicate_context_items = _wrap_dedup_items

_orig_assemble = memory_context.assemble_context
def _wrap_assemble(text, **kwargs):
    result = _orig_assemble(text, **kwargs)
    CURRENT["assemble_intent_kw"] = kwargs.get("intent")
    CURRENT["assemble_prev_topic_terms"] = sorted(kwargs.get("previous_topic_terms") or [])
    CURRENT["assemble_retrieval_override"] = kwargs.get("retrieval_query_override")
    CURRENT["assemble_funnel"] = dict(kwargs.get("funnel") or {})
    CURRENT["final_items"] = [
        {"source": it.source, "text": it.text[:100], "rank_key": it._rank_key()} for it in result.items
    ]
    CURRENT["rendered_block"] = result.render()
    return result
memory_context.assemble_context = _wrap_assemble


# ─────────────────────────────────────────────
# Harness (mirrors tests/test_memory_continuity.py exactly)
# ─────────────────────────────────────────────

def _load_demo(name):
    demo_spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules[name] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo):
    from luno.adapters import MockOpenRouterClient
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text=None, chunk_delay_s=0.0))


def _run_turn(console, demo, text, request_id, conversation_id=None):
    captured = {}
    need_llm = threading.Event()

    def _capture(e):
        if e.get("request_id") != request_id:
            return
        captured["system_prompt"] = e.get("system_prompt")
        captured["messages"] = e.get("messages")
        need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        ok1 = _wait_until(need_llm.is_set, 5.0)
        ok2 = _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0)
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or "", ok1, ok2


def run_conversation(scenario_name, turns, conversation_id="probe-conv"):
    demo = _load_demo(f"main_runtime_demo_probe_{scenario_name}")
    console = _new_console(demo)
    console.start()
    try:
        for i, text in enumerate(turns, start=1):
            _reset_current()
            request_id = f"{scenario_name}-turn-{i}"
            system_prompt, ok1, ok2 = _run_turn(console, demo, text, request_id, conversation_id)
            snapshot = dict(CURRENT)
            snapshot["turn_index"] = i
            snapshot["user_text"] = text
            snapshot["final_system_prompt_len"] = len(system_prompt or "")
            snapshot["final_system_prompt"] = system_prompt
            snapshot["need_llm_ok"] = ok1
            snapshot["pending_cleared_ok"] = ok2
            TURN_LOG.append({"scenario": scenario_name, **snapshot})
    finally:
        console.stop()


SCENARIO_1 = [
    "My mic is an INMP441 connected to an ESP32.",
    "I use ESP32 for the voice system.",
    "Bluetooth isn't available on the plain ESP8266.",
    "I also have an aquascape.",
    "Anyway, what about the mic?",
    "Which one was it again?",
    "What did I use for the ESP32?",
    "How does that connect?",
]

SCENARIO_2_ABC = [
    "My mic is an INMP441 connected to an ESP32.",                 # A1
    "I also have an aquascape with a pump running 24/7.",          # B1
    "I set up WLED on an LED strip in my room.",                   # C1
    "Which mic did I use again?",                                   # A2 (A after B,C)
    "How often does the pump run?",                                 # B2 (B after A,C)
    "What did I use for the LED strip?",                            # C2 (C after A,B)
    "Anyway, what about the mic again?",                            # A3 (A->B->C->A)
]

if __name__ == "__main__":
    run_conversation("scenario1_8turn", SCENARIO_1)
    run_conversation("scenario2_abc", SCENARIO_2_ABC)

    out_path = "/tmp/memory_pipeline_trace.json"
    with open(out_path, "w") as f:
        json.dump(TURN_LOG, f, indent=2, default=str)
    print(f"Wrote {len(TURN_LOG)} turn records to {out_path}")

    # Print a compact human-readable summary
    for rec in TURN_LOG:
        print("=" * 100)
        print(f"[{rec['scenario']}] turn {rec['turn_index']}: {rec['user_text']!r}")
        print(f"  intent={rec['intent']!r} reference_type={rec['reference_type']!r} is_short_followup={rec['is_short_followup']!r}")
        print(f"  topic_history_before={rec['topic_history_before']!r}")
        print(f"  topic_candidates_selected={rec['topic_candidates_selected']!r}")
        print(f"  expanded_retrieval_text={rec['expanded_retrieval_text']!r}")
        print(f"  retriever_raw_candidates={rec['retriever_raw_candidates']!r}")
        print(f"  assemble_funnel={rec['assemble_funnel']!r}")
        final_items = rec.get("final_items") or []
        print(f"  final_items ({len(final_items)}):")
        for it in final_items:
            print(f"    - [{it['source']}] {it['text']!r} rank_key={it['rank_key']}")
        print(f"  rendered_block:\n{rec['rendered_block']}")
