"""
database.py
============

Thin SQLite persistence layer for Vision Memory. This file owns the schema
and raw CRUD only - no tracking, scoring, or event-detection logic lives
here (that's `tracker.py`/`importance.py`/`event_detector.py`); `database.py`
just knows how to save and load the typed records from `models.py`.

Tables
------
- `objects`          one row per persistently-tracked object (present or removed)
- `events`           append-only log of stored (importance >= 3) events
- `world_state`      a SINGLE row holding the latest serialized `WorldState`
- `long_term_memory` promoted habit/pattern statements
- `metadata`         generic key/value store - schema version, event-repeat
                      counters used by `memory.py`'s long-term promotion
                      logic, last short-term-clear timestamp, etc.

Thread safety: `VisionMemory.update()` may be called from a background
capture/watch thread while another thread (e.g. the main conversation loop)
concurrently calls a read method like `get_recent_events()`. SQLite's
default per-thread connection restriction is disabled
(`check_same_thread=False`) and every public method takes a single
`threading.Lock` around its SQL, which is enough for this module's access
pattern (short, infrequent transactions - never held across a network or
model call).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from .models import (
    EventCategory,
    EventRecord,
    LongTermMemoryRecord,
    ObjectStatus,
    TrackedObject,
    WorldState,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    color TEXT,
    location TEXT,
    status TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    importance INTEGER NOT NULL,
    related_object_id TEXT,
    related_human_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);

CREATE TABLE IF NOT EXISTS world_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at TEXT NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS long_term_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement TEXT NOT NULL UNIQUE,
    confidence REAL NOT NULL,
    observation_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_SCHEMA_VERSION = "1"


class Database:
    """Owns one SQLite connection/file. Safe to share across threads."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL = readers don't block the writer and vice versa, and
            # commits are cheaper than the default rollback-journal mode -
            # matters here since `memory.py.update()` is meant to complete
            # in well under 50ms and does several writes per call. NOT every
            # filesystem can do it though - WAL needs shared memory-mapped
            # -wal/-shm sidecar files, which some network drives/mounted/
            # cloud-synced folders can't provide and fail with a raw
            # "disk I/O error" the moment this pragma runs (observed
            # first-hand testing this integration against a mounted drive).
            # Fall back to SQLite's normal default journal mode instead of
            # letting that crash Vision Memory entirely - slightly slower
            # under concurrent access, but fully correct and always works.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as ex:
                print(
                    f"[VisionMemory] ⚠ WAL journal mode nggak didukung drive/folder ini ({ex}) - "
                    "pakai default journal mode SQLite aja (tetap aman, cuma sedikit lebih "
                    "lambat pas ada akses bersamaan)."
                )
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        if self.get_metadata("schema_version") is None:
            self.set_metadata("schema_version", _SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- objects ----------------------------------------------------------

    def upsert_object(self, obj: TrackedObject) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO objects (id, label, color, location, status, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label=excluded.label, color=excluded.color, location=excluded.location,
                    status=excluded.status, last_seen=excluded.last_seen
                """,
                (
                    obj.id, obj.label, obj.color, obj.location, obj.status.value,
                    obj.first_seen.isoformat(), obj.last_seen.isoformat(),
                ),
            )
            self._conn.commit()

    def get_all_objects(self) -> List[TrackedObject]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM objects").fetchall()
        return [self._row_to_object(r) for r in rows]

    def get_present_objects(self) -> List[TrackedObject]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM objects WHERE status = ?", (ObjectStatus.PRESENT.value,)
            ).fetchall()
        return [self._row_to_object(r) for r in rows]

    @staticmethod
    def _row_to_object(row: sqlite3.Row) -> TrackedObject:
        return TrackedObject(
            id=row["id"], label=row["label"], color=row["color"], location=row["location"],
            status=ObjectStatus(row["status"]),
            first_seen=datetime.fromisoformat(row["first_seen"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
        )

    # -- events -------------------------------------------------------------

    def insert_event(self, event: EventRecord) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO events (timestamp, category, description, importance, related_object_id, related_human_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.timestamp.isoformat(), event.category.value, event.description,
                    event.importance, event.related_object_id, event.related_human_id,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_recent_events(self, limit: int = 20, min_importance: int = 1) -> List[EventRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE importance >= ? ORDER BY id DESC LIMIT ?",
                (min_importance, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            id=row["id"], timestamp=datetime.fromisoformat(row["timestamp"]),
            category=EventCategory(row["category"]), description=row["description"],
            importance=row["importance"], related_object_id=row["related_object_id"],
            related_human_id=row["related_human_id"],
        )

    # -- world_state (singleton row) -----------------------------------------

    def save_world_state(self, state: WorldState) -> None:
        payload = json.dumps(state.to_dict(), ensure_ascii=False)
        updated_at = (state.updated_at or datetime.now(timezone.utc)).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO world_state (id, updated_at, state_json) VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, state_json=excluded.state_json
                """,
                (updated_at, payload),
            )
            self._conn.commit()

    def load_world_state(self) -> Optional[WorldState]:
        with self._lock:
            row = self._conn.execute("SELECT state_json FROM world_state WHERE id = 1").fetchone()
        if row is None:
            return None
        return WorldState.from_dict(json.loads(row["state_json"]))

    def clear_world_state(self) -> None:
        """Used by `memory.VisionMemory.clear_short_memory()` - deletes the
        singleton row AND every `objects` row, since tracked objects are
        short-term-memory state too (long-term habits live in
        `long_term_memory`, untouched by this)."""
        with self._lock:
            self._conn.execute("DELETE FROM world_state")
            self._conn.execute("DELETE FROM objects")
            self._conn.commit()

    # -- long_term_memory -----------------------------------------------------

    def upsert_long_term_memory(self, record: LongTermMemoryRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO long_term_memory (statement, confidence, observation_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(statement) DO UPDATE SET
                    confidence=excluded.confidence,
                    observation_count=excluded.observation_count,
                    updated_at=excluded.updated_at
                """,
                (
                    record.statement, record.confidence, record.observation_count,
                    record.created_at.isoformat(), record.updated_at.isoformat(),
                ),
            )
            self._conn.commit()

    def get_long_term_memory(self) -> List[LongTermMemoryRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM long_term_memory ORDER BY confidence DESC, observation_count DESC"
            ).fetchall()
        return [
            LongTermMemoryRecord(
                id=r["id"], statement=r["statement"], confidence=r["confidence"],
                observation_count=r["observation_count"],
                created_at=datetime.fromisoformat(r["created_at"]),
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]

    # -- metadata (generic key/value) ------------------------------------------

    def get_metadata(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def set_metadata(self, key: str, value: Any) -> None:
        # ALWAYS JSON-encode (even plain strings) so get_metadata's
        # json.loads() round-trips consistently for every value type -
        # storing a bare string "1" without encoding would come back as the
        # int 1 on read instead of the string "1".
        payload = json.dumps(value)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, payload),
            )
            self._conn.commit()
