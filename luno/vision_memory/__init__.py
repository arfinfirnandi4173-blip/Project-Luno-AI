"""
Vision Memory
=============

Persistent visual awareness for Luno - sits between the vision model
(Gemini 2.0 Flash, see `luno/vision.py`) and the LLM, turning a stream of raw
per-frame descriptions into: an always-current world state, a log of only
the MEANINGFUL changes (scored, deduplicated), and slowly-learned habits.
The LLM only ever sees `get_world_state()`/`get_recent_events()`-sized
summaries, never a raw play-by-play of every frame.

    Camera -> OpenCV -> Gemini 2.0 Flash -> Vision Memory -> GPT -> TTS -> avatar
                                                 ^^^^^^^^^^^^^
                                                 this package

Quick start
-----------
    from luno import vision_memory

    events = vision_memory.update("Vinn is sitting in front of the computer. "
                                   "There is a white cup on the desk.")
    for e in events:
        print(e.importance, e.description)

    state = vision_memory.get_world_state()
    print(state.objects)   # {"cup#1": TrackedObject(...), "desk#1": ...}

Architecture (see each file's own docstring for the full story)
-----------------------------------------------------------------
    models.py          typed data structures (input + state)
    database.py         SQLite persistence (objects/events/world_state/
                         long_term_memory/metadata tables)
    tracker.py           persistent-id object/human tracking
    importance.py        1-5 change scoring, threshold=3
    event_detector.py    diff old vs new state -> scored, deduped events
    scene_graph.py        spatial relations + "where is my X" queries
    memory.py             orchestrator (4 memory layers, long-term promotion)
    utils.py               heuristic text->SceneObservation parser + helpers
    api.py                  the module-level facade re-exported below

Swapping in a better vision source later (e.g. one that emits structured
JSON instead of free text) only requires producing `models.SceneObservation`
objects directly instead of going through `utils.parse_description_heuristic`
- nothing else in this package needs to change.
"""

from .api import (
    clear_short_memory,
    configure,
    export_json,
    get_long_term_memory,
    get_recent_events,
    get_world_state,
    query_location,
    reset,
    update,
)
from .models import (
    EventCategory,
    EventRecord,
    HumanActivity,
    HumanObservation,
    LongTermMemoryRecord,
    ObjectObservation,
    ObjectStatus,
    RoomObservation,
    SceneObservation,
    SceneRelation,
    TrackedHuman,
    TrackedObject,
    WorldState,
)

__all__ = [
    # api.py
    "update", "get_recent_events", "get_world_state", "get_long_term_memory",
    "clear_short_memory", "export_json", "configure", "reset", "query_location",
    # models.py
    "SceneObservation", "ObjectObservation", "HumanObservation", "RoomObservation",
    "WorldState", "TrackedObject", "TrackedHuman", "SceneRelation",
    "EventRecord", "LongTermMemoryRecord", "EventCategory", "HumanActivity", "ObjectStatus",
]
