"""
api.py
======

The public facade for Vision Memory - the ONLY module callers outside this
package should import from (re-exported by `__init__.py` so
`from luno import vision_memory` gives you these functions directly, per
the spec's requested call style):

    vision_memory.update(scene)
    vision_memory.get_recent_events()
    vision_memory.get_world_state()
    vision_memory.get_long_term_memory()
    vision_memory.clear_short_memory()
    vision_memory.export_json()

Owns a lazily-created module-level `VisionMemory` singleton, so callers
never need to construct or thread one through themselves - matching the
simple function-call style already used elsewhere in this codebase (see
`luno/vision.py`, `luno/memory.py`).

`update(scene)` accepts THREE input shapes so it can sit directly
downstream of this project's current Gemini vision integration
(`luno/vision.py`, which produces free-text descriptions, not structured
JSON) without requiring a parsing step at every call site:

    vision_memory.update("Vinn is sitting in front of the computer.")
    vision_memory.update({"timestamp": "...", "description": "..."})
    vision_memory.update(my_scene_observation)  # models.SceneObservation, if
                                                  # you already have structured data
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .memory import VisionMemory
from .models import EventRecord, LongTermMemoryRecord, SceneObservation, WorldState
from .scene_graph import query_location as _query_location
from .utils import ensure_aware, parse_description_heuristic, utcnow

_instance: Optional[VisionMemory] = None
_instance_lock = threading.Lock()
_known_identity: Optional[str] = None
_db_path_override: Optional[str] = None


def configure(db_path: Optional[str] = None, known_identity: Optional[str] = None) -> None:
    """Optional - call once before the first `update()` if you want to
    override where the SQLite file lives, or tell the default heuristic
    parser who the user is (skips its fragile capitalized-word identity
    guess - see `utils.parse_description_heuristic`). Safe to call again
    later; takes effect on the NEXT `update()` call, does not retroactively
    change already-loaded state."""
    global _known_identity, _db_path_override
    if db_path is not None:
        _db_path_override = db_path
    if known_identity is not None:
        _known_identity = known_identity


def _default_db_path() -> str:
    try:
        from .. import config as luno_config
        return str(Path(luno_config.DATA_DIR) / "vision_memory.sqlite3")
    except Exception:
        # Standalone use outside the Luno project (no `luno.config` module
        # available) - fall back to a local file instead of failing import.
        return "vision_memory.sqlite3"


def _get_memory() -> VisionMemory:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = VisionMemory(_db_path_override or _default_db_path())
    return _instance


def reset() -> None:
    """Drop the current singleton so the next call re-creates it (re-reads
    from disk). Mainly useful for tests; not needed in normal operation."""
    global _instance
    with _instance_lock:
        _instance = None


def _coerce_observation(scene: Union[SceneObservation, Dict[str, Any], str]) -> SceneObservation:
    if isinstance(scene, SceneObservation):
        return scene
    if isinstance(scene, dict):
        timestamp = _parse_timestamp(scene.get("timestamp"))
        description = str(scene.get("description", ""))
        return parse_description_heuristic(description, timestamp=timestamp, known_identity=_known_identity)
    if isinstance(scene, str):
        return parse_description_heuristic(scene, timestamp=utcnow(), known_identity=_known_identity)
    raise TypeError(f"vision_memory.update() expects a SceneObservation, dict, or str - got {type(scene).__name__}")


def _parse_timestamp(value: Any) -> datetime:
    # Every path here must return a timezone-AWARE datetime - see
    # utils.ensure_aware's docstring. Caller-supplied ISO strings (like the
    # spec's own "2026-07-24T18:00:12" example) commonly have no UTC offset,
    # which would otherwise crash later comparisons against aware timestamps
    # produced internally via utcnow().
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        return ensure_aware(value)
    try:
        return ensure_aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return utcnow()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update(scene: Union[SceneObservation, Dict[str, Any], str]) -> List[EventRecord]:
    """Ingest one new vision observation. Returns the list of NEW events
    (already scored >= importance threshold and deduplicated) this call
    produced - usually empty, since most frames change nothing meaningful."""
    observation = _coerce_observation(scene)
    return _get_memory().update(observation)


def get_recent_events(limit: int = 20, min_importance: int = 3) -> List[EventRecord]:
    return _get_memory().get_recent_events(limit=limit, min_importance=min_importance)


def get_world_state() -> WorldState:
    return _get_memory().get_world_state()


def get_long_term_memory() -> List[LongTermMemoryRecord]:
    return _get_memory().get_long_term_memory()


def clear_short_memory() -> None:
    _get_memory().clear_short_memory()


def export_json(event_limit: int = 50) -> str:
    return _get_memory().export_json(event_limit=event_limit)


def query_location(label_or_id: str) -> Optional[str]:
    """Facade wrapper around `scene_graph.query_location()` - answers "where
    is my <label>" directly from the CACHED world state, with no vision model
    call involved. Added specifically so integration callers (see
    `luno/vision.py`) can do `vision_memory.query_location("cup")` without
    reaching into `scene_graph` themselves; behavior is unchanged from
    `scene_graph.query_location`, this just binds it to the current
    singleton's world state. Returns None if nothing matching is currently
    tracked as present - callers should treat that as "memory doesn't know,
    fall back to actually asking the vision model", not as an error."""
    return _query_location(label_or_id, get_world_state())
