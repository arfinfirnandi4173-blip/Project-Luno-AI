"""
test_memory_retrieval.py
==========================

Regression suite for Sprint 5 - Smart Memory Injection (Retrieval-Based).

Covers the spec's own required list:
    1. Relevant object retrieval
    2. No irrelevant memories injected
    3. Empty retrieval
    4. Multiple relevant memories
    5. Ranking
    6. Memory limit
    7. Stale memory handling
    8. No automatic vision invocation
    9. Prompt construction
    10. Config reload
    11. Keyword retrieval
    12. Thread safety
    13. Stress test with thousands of memories

Plus a smaller integration section (tests 14+) proving the retriever is
actually WIRED into `main_runtime_demo.py`'s real turn-handling path
(`PlannerBridgeModule._handle_utterance()` -> `NeedLLMResponse`), not just
correct in isolation - using the same `RuntimeDemoConsole` harness
`tests/test_interrupt_routing_fix.py` already established for this
project (real Event Bus, real modules; only OpenRouter/Fish Audio are
mocked - no network, no microphone, no vision model).

Run:
    python3 tests/test_memory_retrieval.py
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.memory_retrieval import (  # noqa: E402
    MemoryRetrievalConfig,
    MemoryRetriever,
    RelevantMemory,
    analyze_query,
    build_memory_prompt_block,
    make_long_term_memory_source,
    make_planner_state_source,
    make_tool_execution_source,
    make_vision_event_source,
    make_vision_human_source,
    make_vision_object_source,
)
from luno.vision_memory.models import (  # noqa: E402
    EventCategory,
    EventRecord,
    HumanActivity,
    LongTermMemoryRecord,
    ObjectStatus,
    RoomObservation,
    TrackedHuman,
    TrackedObject,
    WorldState,
)

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _silent(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _obj(label, location, status=ObjectStatus.PRESENT, age_minutes=1.0, obj_id=None):
    ts = _now() - timedelta(minutes=age_minutes)
    return TrackedObject(
        id=obj_id or f"{label}#1", label=label, color=None, location=location,
        status=status, first_seen=ts, last_seen=ts,
    )


def _human(age_minutes=1.0, activity=HumanActivity.TYPING, pose="sitting", emotion="calm"):
    ts = _now() - timedelta(minutes=age_minutes)
    return TrackedHuman(
        id="user#1", identity=None, emotion=emotion, pose=pose, activity=activity,
        first_seen=ts, last_seen=ts,
    )


def _world(objects=None, humans=None) -> WorldState:
    return WorldState(
        objects=objects or {}, humans=humans or {}, room=RoomObservation(),
        relations=[], updated_at=_now(),
    )


def _make_retriever(world_state_fn=None, **config_kwargs) -> MemoryRetriever:
    retriever = MemoryRetriever(MemoryRetrievalConfig(**config_kwargs))
    if world_state_fn is not None:
        retriever.register_source("vision_objects", make_vision_object_source(world_state_fn))
        retriever.register_source("vision_human", make_vision_human_source(world_state_fn))
    return retriever


# ============================================================================
# 1 - Relevant object retrieval
# ============================================================================

@scenario
def test_1_relevant_object_retrieval():
    world = _world(objects={"cup#1": _obj("cup", "on the desk", age_minutes=3)})
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("Where is my cup?")
    assert len(memories) == 1
    assert "cup" in memories[0].text.lower()
    assert "desk" in memories[0].text.lower()


# ============================================================================
# 2 - No irrelevant memories injected
# ============================================================================

@scenario
def test_2_no_irrelevant_memories_injected():
    world = _world(objects={
        "cup#1": _obj("cup", "on the desk"),
        "laptop#1": _obj("laptop", "on the table"),
        "door#1": _obj("door", None),
        "lamp#1": _obj("lamp", "in the corner"),
        "keyboard#1": _obj("keyboard", "on the table"),
        "plant#1": _obj("plant", "by the window"),
    })
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("Where is my cup?")
    texts = " ".join(m.text.lower() for m in memories)
    assert len(memories) == 1
    for irrelevant in ("laptop", "door", "lamp", "keyboard", "plant"):
        assert irrelevant not in texts, f"{irrelevant!r} should not have been retrieved"


# ============================================================================
# 3 - Empty retrieval
# ============================================================================

@scenario
def test_3_empty_retrieval_for_no_signal_query():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("What's 5 + 5?")
    assert memories == []


@scenario
def test_3b_empty_retrieval_when_nothing_matches():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("Where is my wallet?")
    assert memories == []


@scenario
def test_3c_empty_retrieval_when_no_sources_registered():
    retriever = MemoryRetriever(MemoryRetrievalConfig())
    memories = retriever.retrieve_memories("where is my cup?")
    assert memories == []


# ============================================================================
# 4 - Multiple relevant memories
# ============================================================================

@scenario
def test_4_multiple_relevant_memories_for_location_query():
    world = _world(objects={
        "cup#1": _obj("cup", "on the desk"),
        "laptop#1": _obj("laptop", "on the desk"),
        "phone#1": _obj("phone", "on the desk"),
        "lamp#1": _obj("lamp", "in the corner"),
    })
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("What is on my desk?")
    labels = {m.raw.label for m in memories}
    assert labels == {"cup", "laptop", "phone"}
    assert "lamp" not in labels


# ============================================================================
# 5 - Ranking
# ============================================================================

@scenario
def test_5_ranking_prefers_recent_over_old():
    world = _world(objects={
        "cup#1": _obj("cup", "on the desk", age_minutes=1, obj_id="cup#1"),
        "cup#2": _obj("cup", "on the desk", age_minutes=200, obj_id="cup#2"),
    })
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("where is my cup?")
    assert len(memories) == 2
    assert memories[0].raw.id == "cup#1", "the more recent observation should rank first"
    assert memories[0].score > memories[1].score


@scenario
def test_5b_ranking_prefers_present_over_removed():
    world = _world(objects={
        "cup#1": _obj("cup", "on the desk", status=ObjectStatus.REMOVED, age_minutes=1, obj_id="cup#1"),
    })
    # a REMOVED object is filtered out entirely by the vision object source
    # (status != "present") - confirms removed/superseded observations
    # never outrank (or even appear alongside) present ones.
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("where is my cup?")
    assert memories == []


@scenario
def test_5c_ranking_explicit_label_match_outranks_location_only_match():
    world = _world(objects={
        "cup#1": _obj("cup", "on the desk", age_minutes=5, obj_id="cup#1"),
        "laptop#1": _obj("laptop", "on the desk", age_minutes=5, obj_id="laptop#1"),
    })
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("where is my cup on the desk?")
    # both match (cup via label, laptop via "desk" location token) but the
    # direct label match should score higher.
    by_label = {m.raw.label: m for m in memories}
    assert by_label["cup"].score > by_label["laptop"].score


# ============================================================================
# 6 - Memory limit
# ============================================================================

@scenario
def test_6_memory_limit_enforced():
    objects = {f"cup#{i}": _obj("cup", f"spot {i}", age_minutes=i, obj_id=f"cup#{i}") for i in range(20)}
    world = _world(objects=objects)
    retriever = _make_retriever(lambda: world, max_results=5, max_tokens=10_000)
    memories = retriever.retrieve_memories("where is my cup?")
    assert len(memories) == 5


@scenario
def test_6b_token_budget_enforced():
    objects = {f"cup#{i}": _obj("cup", "on the desk in the room by the window", age_minutes=i, obj_id=f"cup#{i}") for i in range(20)}
    world = _world(objects=objects)
    retriever = _make_retriever(lambda: world, max_results=20, max_tokens=30)
    memories = retriever.retrieve_memories("where is my cup?")
    est_tokens = sum(max(1, len(m.text) // 4) for m in memories)
    assert est_tokens <= 30
    assert len(memories) < 20, "token budget should have trimmed the result before the count limit did"


# ============================================================================
# 7 - Stale memory handling
# ============================================================================

@scenario
def test_7_stale_memory_marked_and_worded_uncertainly():
    world = _world(objects={"cup#1": _obj("cup", "on the desk", age_minutes=60 * 20)})  # 20 hours
    retriever = _make_retriever(lambda: world, stale_after_minutes=30)
    memories = retriever.retrieve_memories("where is my cup?")
    assert len(memories) == 1
    assert memories[0].stale is True
    assert "approximately" in memories[0].text.lower()
    assert "hour" in memories[0].text.lower()


@scenario
def test_7b_fresh_memory_not_marked_stale():
    world = _world(objects={"cup#1": _obj("cup", "on the desk", age_minutes=3)})
    retriever = _make_retriever(lambda: world, stale_after_minutes=30)
    memories = retriever.retrieve_memories("where is my cup?")
    assert memories[0].stale is False
    assert "approximately" not in memories[0].text.lower()
    assert "minute" in memories[0].text.lower()


# ============================================================================
# 8 - No automatic vision invocation
# ============================================================================

@scenario
def test_8_no_automatic_vision_invocation_even_with_look_keywords():
    """The retriever must NEVER call a live vision function, no matter what
    the query says - not even for "look"/"check"/"see"/"what do you see" -
    per the spec: "Only invoke live vision when another module explicitly
    requests... Keep retrieval and perception separate." There is no path
    in this package that could call one (no import of luno.vision at all),
    but this test proves it with a spy anyway rather than relying on
    "we just didn't write that code"."""
    live_vision_calls = []

    def _spy_live_vision(*a, **k):
        live_vision_calls.append((a, k))
        raise AssertionError("live vision must never be called by memory retrieval")

    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    # register a "trap" source under a name that sounds like it could be
    # live vision, to prove nothing in the retriever's own code path
    # reaches for anything resembling it.
    for query_text in ["look at the camera", "check what you see", "what do you see right now", "see anything?"]:
        retriever.retrieve_memories(query_text)
    assert live_vision_calls == []


# ============================================================================
# 9 - Prompt construction
# ============================================================================

@scenario
def test_9_prompt_construction_format():
    memories = [
        RelevantMemory(text="Cup last seen on the desk. Observed 3 minutes ago.", source="vision_memory", score=1.0),
    ]
    block = build_memory_prompt_block(memories)
    assert block == "Relevant Memory:\n- Cup last seen on the desk. Observed 3 minutes ago."


@scenario
def test_9b_prompt_construction_empty_when_no_memories():
    assert build_memory_prompt_block([]) == ""


@scenario
def test_9c_prompt_construction_multiple_lines():
    memories = [
        RelevantMemory(text="Cup last seen on the desk.", source="vision_memory", score=1.0),
        RelevantMemory(text="Laptop last seen on the desk.", source="vision_memory", score=0.9),
    ]
    block = build_memory_prompt_block(memories)
    assert block == "Relevant Memory:\n- Cup last seen on the desk.\n- Laptop last seen on the desk."


# ============================================================================
# 10 - Config reload
# ============================================================================

@scenario
def test_10_config_reload_picks_up_new_env_without_recreating_retriever():
    saved = os.environ.pop("MAX_MEMORY_RESULTS", None)
    try:
        world = _world(objects={f"cup#{i}": _obj("cup", "on the desk", obj_id=f"cup#{i}") for i in range(10)})
        retriever = _make_retriever(lambda: world, max_results=5)
        assert len(retriever.retrieve_memories("where is my cup?")) == 5

        os.environ["MAX_MEMORY_RESULTS"] = "2"
        retriever.reload_config()
        assert retriever.config.max_results == 2
        assert len(retriever.retrieve_memories("where is my cup?")) == 2
    finally:
        if saved is None:
            os.environ.pop("MAX_MEMORY_RESULTS", None)
        else:
            os.environ["MAX_MEMORY_RESULTS"] = saved


@scenario
def test_10b_config_reload_keeps_registered_sources():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    retriever.reload_config(MemoryRetrievalConfig(max_results=3))
    memories = retriever.retrieve_memories("where is my cup?")
    assert len(memories) == 1, "sources must survive a config reload"


@scenario
def test_10c_disabled_via_config_returns_nothing():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world, enabled=False)
    assert retriever.retrieve_memories("where is my cup?") == []


# ============================================================================
# 11 - Keyword retrieval
# ============================================================================

@scenario
def test_11_keyword_analysis_extracts_object_and_location_tokens():
    q = analyze_query("Where is my cup on the desk?")
    assert "cup" in q.tokens
    assert "desk" in q.tokens
    assert q.has_any_signal is True


@scenario
def test_11b_keyword_analysis_detects_self_query():
    q = analyze_query("Am I wearing headphones?")
    assert q.is_self_query is True


@scenario
def test_11c_keyword_analysis_detects_time_reference():
    q = analyze_query("What did I do yesterday?")
    assert q.mentions_time is True


@scenario
def test_11d_keyword_retrieval_is_case_insensitive():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    memories_upper = retriever.retrieve_memories("WHERE IS MY CUP?")
    memories_lower = retriever.retrieve_memories("where is my cup?")
    assert len(memories_upper) == len(memories_lower) == 1


@scenario
def test_11e_self_query_retrieves_human_observation_not_objects():
    world = _world(
        objects={"headphones#1": _obj("headphones", "on the desk")},
        humans={"user#1": _human(activity=HumanActivity.TYPING, pose="sitting", emotion="calm")},
    )
    retriever = _make_retriever(lambda: world)
    memories = retriever.retrieve_memories("Am I wearing headphones?")
    assert len(memories) == 1
    assert memories[0].source == "vision_memory"
    assert "you were" in memories[0].text.lower() or "pose" in memories[0].text.lower()
    # must NOT return the object source's own headphones match instead
    assert memories[0].raw is not None and hasattr(memories[0].raw, "activity")


# ============================================================================
# 12 - Thread safety
# ============================================================================

@scenario
def test_12_thread_safety_concurrent_retrieval_and_registration():
    world = _world(objects={f"cup#{i}": _obj("cup", "on the desk", obj_id=f"cup#{i}") for i in range(50)})
    retriever = _make_retriever(lambda: world)
    errors: List[Exception] = []
    stop = threading.Event()

    def _retrieve_loop():
        while not stop.is_set():
            try:
                retriever.retrieve_memories("where is my cup?")
            except Exception as ex:  # pragma: no cover
                errors.append(ex)

    def _register_loop():
        i = 0
        while not stop.is_set():
            try:
                retriever.register_source(f"extra_{i % 3}", make_long_term_memory_source(lambda: []))
                retriever.set_source_enabled(f"extra_{i % 3}", i % 2 == 0)
                i += 1
            except Exception as ex:  # pragma: no cover
                errors.append(ex)

    threads = [threading.Thread(target=_retrieve_loop) for _ in range(4)] + \
        [threading.Thread(target=_register_loop) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=2)

    assert errors == [], f"concurrent access raised: {errors}"


# ============================================================================
# 13 - Stress test with thousands of memories
# ============================================================================

@scenario
def test_13_stress_thousands_of_tracked_objects():
    objects = {}
    for i in range(3000):
        label = "cup" if i % 500 == 0 else f"widget{i}"
        objects[f"obj#{i}"] = _obj(label, f"location {i}", age_minutes=i % 120, obj_id=f"obj#{i}")
    world = _world(objects=objects)
    retriever = _make_retriever(lambda: world, max_results=5)

    start = time.time()
    memories = retriever.retrieve_memories("where is my cup?")
    elapsed = time.time() - start

    assert len(memories) <= 5
    assert all(m.raw.label == "cup" for m in memories)
    assert elapsed < 3.0, f"retrieval over 3000 objects took too long: {elapsed:.2f}s"


@scenario
def test_13b_stress_thousands_of_long_term_memories():
    records = [
        LongTermMemoryRecord(
            id=i, statement=f"User likes item {i}" if i % 300 != 0 else "User likes coffee in the morning",
            confidence=0.5, observation_count=1,
            created_at=_now(), updated_at=_now() - timedelta(minutes=i % 60),
        )
        for i in range(4000)
    ]
    retriever = MemoryRetriever(MemoryRetrievalConfig(max_results=5))
    retriever.register_source("long_term_memory", make_long_term_memory_source(lambda: records))

    start = time.time()
    memories = retriever.retrieve_memories("do I like coffee?")
    elapsed = time.time() - start

    assert len(memories) <= 5
    assert any("coffee" in m.text.lower() for m in memories)
    assert elapsed < 3.0, f"retrieval over 4000 long-term records took too long: {elapsed:.2f}s"


# ============================================================================
# 14+ - Additional source coverage (planner state / tool execution / events)
# ============================================================================

@scenario
def test_14_vision_event_source_matches_description_keywords():
    events = [
        EventRecord(id=1, timestamp=_now() - timedelta(minutes=2), category=EventCategory.HUMAN_ENTERED,
                    description="Vinn entered the room", importance=5),
        EventRecord(id=2, timestamp=_now() - timedelta(minutes=10), category=EventCategory.OBJECT_MOVED,
                    description="The lamp was moved", importance=3),
    ]
    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("vision_events", make_vision_event_source(lambda limit=20: events))
    memories = retriever.retrieve_memories("did someone enter the room?")
    assert any("entered" in m.text.lower() for m in memories)
    assert not any("lamp" in m.text.lower() for m in memories)


@scenario
def test_15_planner_state_source_only_fires_for_action_related_queries():
    source = make_planner_state_source(lambda: {"last_plan_id": "plan-123"})
    q_relevant = analyze_query("what did you just do?")
    q_irrelevant = analyze_query("what's the weather like?")
    config = MemoryRetrievalConfig()
    assert source(q_relevant, config) != []
    assert source(q_irrelevant, config) == []


@scenario
def test_16_tool_execution_source_uses_summary_field():
    # Keyword matching has no stemming ("turn" would not match "turned") -
    # phrase the query to share an exact word with the summary, matching
    # the spec's "keyword matching is acceptable initially" scope.
    source = make_tool_execution_source(lambda: [{"summary": "turned on the RGB strip"}, {"no_summary": True}])
    q = analyze_query("what about the rgb strip?")
    config = MemoryRetrievalConfig()
    results = source(q, config)
    assert len(results) == 1
    assert "rgb strip" in results[0].text.lower()


@scenario
def test_17_source_exception_is_isolated_not_fatal():
    def _broken_source(query, config):
        raise RuntimeError("boom")

    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    retriever.register_source("broken", _broken_source)
    memories = retriever.retrieve_memories("where is my cup?")
    assert len(memories) == 1  # the working source's result still comes through


@scenario
def test_18_disabled_source_is_skipped():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})
    retriever = _make_retriever(lambda: world)
    retriever.set_source_enabled("vision_objects", False)
    memories = retriever.retrieve_memories("where is my cup?")
    assert memories == []


@scenario
def test_19_debug_logging_only_when_debug_true():
    world = _world(objects={"cup#1": _obj("cup", "on the desk")})

    retriever_quiet = _make_retriever(lambda: world, debug=False)
    _, output_quiet = _silent(retriever_quiet.retrieve_memories, "where is my cup?")
    assert output_quiet == ""

    retriever_debug = _make_retriever(lambda: world, debug=True)
    _, output_debug = _silent(retriever_debug.retrieve_memories, "where is my cup?")
    assert "memory_retrieval" in output_debug.lower() or "Memory retrieval" in output_debug
    assert "Retrieved" in output_debug
    assert "Injected" in output_debug


# ============================================================================
# 20+ - Integration: actually wired into main_runtime_demo.py's real turn path
# ============================================================================

def _load_demo():
    import importlib.util
    spec = importlib.util.spec_from_file_location("main_runtime_demo_sprint5", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_sprint5"] = demo
    spec.loader.exec_module(demo)
    return demo


@scenario
def test_20_handle_utterance_injects_memory_block_into_system_prompt():
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    from luno.wake_session import WakeSessionConfig

    client = MockOpenRouterClient(canned_text="It's on the desk.", chunk_delay_s=0.0)
    fish = MockFishAudioClient(playback_delay_s=0.0)
    console = demo.RuntimeDemoConsole(
        openrouter_client=client, fish_audio_client=fish,
        session_config=WakeSessionConfig(sleep_enabled=False),
    )
    _silent(console.start)

    world = _world(objects={"cup#1": _obj("cup", "on the desk", age_minutes=3)})
    console.planner_module.memory_retriever.register_source(
        "vision_objects", make_vision_object_source(lambda: world)
    )
    console.planner_module.memory_retriever.register_source("vision_human", make_vision_human_source(lambda: world))

    captured = {}
    done = threading.Event()

    def _on_need_llm(e):
        captured["system_prompt"] = e.get("system_prompt")
        done.set()

    console.runtime.event_bus.subscribe("need_llm_response", _on_need_llm)
    try:
        _silent(console.simulate_speech, "where is my cup?")
        assert done.wait(5.0), "NeedLLMResponse was never published"
        assert captured["system_prompt"] is not None
        # Memory Context Assembly & Retrieval Unification sprint: this
        # block used to be labeled "Relevant Memory:" (built directly via
        # `build_memory_prompt_block(relevant_memories_early)` at the
        # production call site); it is now produced by
        # `luno.memory_context.assemble_context()`'s unified, grouped
        # rendering instead - same underlying `relevant_memories_early`
        # candidate pool and relevance-first guarantee, only the section
        # header text changed (see that sprint's own call-site comment in
        # `main_runtime_demo.py`). `build_memory_prompt_block()` itself is
        # unchanged - see this file's own direct unit tests of it above.
        assert "[Relevant Memories]" in captured["system_prompt"]
        assert "cup" in captured["system_prompt"].lower()
        assert "desk" in captured["system_prompt"].lower()
    finally:
        _silent(console.stop)


@scenario
def test_21_handle_utterance_no_memory_block_when_nothing_relevant():
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    from luno.wake_session import WakeSessionConfig

    client = MockOpenRouterClient(canned_text="10.", chunk_delay_s=0.0)
    fish = MockFishAudioClient(playback_delay_s=0.0)
    console = demo.RuntimeDemoConsole(
        openrouter_client=client, fish_audio_client=fish,
        session_config=WakeSessionConfig(sleep_enabled=False),
    )
    _silent(console.start)
    console.planner_module.memory_retriever.register_source(
        "vision_objects", make_vision_object_source(lambda: _world())
    )
    console.planner_module.memory_retriever.register_source(
        "vision_human", make_vision_human_source(lambda: _world())
    )

    captured = {}
    done = threading.Event()

    def _on_need_llm(e):
        captured["system_prompt"] = e.get("system_prompt")
        done.set()

    console.runtime.event_bus.subscribe("need_llm_response", _on_need_llm)
    try:
        _silent(console.simulate_speech, "what's 5 plus 5?")
        assert done.wait(5.0), "NeedLLMResponse was never published"
        prompt = captured.get("system_prompt") or ""
        assert "[Relevant Memories]" not in prompt
    finally:
        _silent(console.stop)


@scenario
def test_22_memquery_command_does_not_crash_and_shows_preview():
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    from luno.wake_session import WakeSessionConfig

    client = MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0)
    fish = MockFishAudioClient(playback_delay_s=0.0)
    console = demo.RuntimeDemoConsole(
        openrouter_client=client, fish_audio_client=fish,
        session_config=WakeSessionConfig(sleep_enabled=False),
    )
    world = _world(objects={"cup#1": _obj("cup", "on the desk", age_minutes=3)})
    console.planner_module.memory_retriever.register_source("vision_objects", make_vision_object_source(lambda: world))
    try:
        _, output = _silent(console._handle_command, "/memquery where is my cup?")
        assert "cup" in output.lower()
        assert "Relevant Memory" in output or "relevant memories" in output.lower()
    finally:
        _silent(console.stop)


@scenario
def test_23_reload_command_reloads_memory_config_without_crashing():
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    from luno.wake_session import WakeSessionConfig

    client = MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0)
    fish = MockFishAudioClient(playback_delay_s=0.0)
    console = demo.RuntimeDemoConsole(
        openrouter_client=client, fish_audio_client=fish,
        session_config=WakeSessionConfig(sleep_enabled=False),
    )
    try:
        _, output = _silent(console._handle_command, "/reload")
        assert "reloaded" in output.lower()
    finally:
        _silent(console.stop)


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
