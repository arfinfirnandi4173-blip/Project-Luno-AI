"""
memory_guard.py
================

Memory Guard - "Memory stores verified facts, not generated language."

Deliberately a NEW, standalone module - does NOT modify `luno/memory.py`,
`luno/planner/executor.py`, `main_runtime_demo.py::build_verified_action_notes()`,
or anything under `luno/tool_manager`/`ToolResult` (per this sprint's own
"Jangan mengubah implementasi tersebut"). Those are exactly what already
make this module's job possible:

  - `ToolResult.success`/`.data` are already the honest, verified truth
    (Reliability Sprint - `luno/tool_manager/builtin/real_home_assistant.py`
    only sets `success=True` once the real device state has been
    re-read and matches what was requested).
  - `build_verified_action_notes()` already keeps the LLM from lying
    about them (Never Assume Success sprint).
  - `Task.result` already survives a FAILED task too, not just
    COMPLETED (Executor fix, same sprint).

None of that persists anywhere as a durable fact about the world,
though - it all lives and dies with one conversation turn. This module
is that missing, minimal piece.

Two clearly separated stores (Bagian 2 of this sprint's spec):

  - `luno.memory.session_log` / `remember_turn()` (untouched,
    pre-existing) is CONVERSATION HISTORY - "I tried to turn on the
    light, but it didn't respond" belongs there, always, success or
    not. It is never read as a fact about device state, and this
    module never touches it.
  - `VerifiedFactStore` (here, new) is FACTS ABOUT THE WORLD - only
    ever written from a verified `ToolResult`, keyed by `entity_id`,
    NEVER from LLM-generated text (Bagian 3/4: the LLM's reply text is
    never read by this module at all - there is no code path here that
    could "believe" it over the `ToolResult`, because it's simply never
    looked at).

Nothing here calls Home Assistant, an LLM, or does any I/O beyond one
small local JSON file (mirrors `luno.memory`'s own
`long_term_memory.json` persistence pattern/location, so restarting
Luno doesn't forget verified device state) - see this sprint's own
"Performance" section.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config
from . import persistence


def _log(message: str) -> None:
    """Matches `luno/memory.py`'s own logging convention (plain
    `print()`, no dedicated logger for this legacy-facing side of the
    codebase) rather than importing a logger from another package -
    keeps this module dependency-free per the sprint's own
    "Performance"/"tidak boleh ada dependency baru" rule."""
    print(f"[MemoryGuard] {message}")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _field(tool_result: Any, key: str) -> Any:
    """Duck-typed field access - accepts a real `ToolResult` (attribute
    access) or the dict shape it's already serialized to everywhere
    else in this codebase (`ToolResult.to_dict()` - `task.result`,
    `ToolFinished`/`tool_failed` event `.data`, ...), without forcing
    either side to convert first."""
    if tool_result is None:
        return None
    if isinstance(tool_result, dict):
        return tool_result.get(key)
    return getattr(tool_result, key, None)


# ─────────────────────────────────────────────
#  Bagian 1: the Memory Guard itself
# ─────────────────────────────────────────────

def should_store_verified_result(tool_result: Any) -> bool:
    """The one gate every verified-fact write must pass through. Fails
    CLOSED for anything that isn't an unambiguous, verified success:
    missing input, an unreadable shape, or `success` not being exactly
    `True` all block storage.

    Deliberately does NOT special-case "verification_failed" /
    "timeout" / "offline" / "entity_unavailable" strings - the
    Reliability Sprint already makes `ToolResult.success` False in
    every one of those cases (see
    `luno/tool_manager/builtin/real_home_assistant.py`), so checking
    `success is True` alone already covers all of them without this
    guard needing to know (or stay in sync with) that specific list."""
    return _field(tool_result, "success") is True


def _blocked_reason(tool_result: Any) -> str:
    """Best-effort human-readable reason for a BLOCK log line (Bagian
    8) - never used as a matching/gating condition, purely
    observability."""
    data = _field(tool_result, "data")
    if isinstance(data, dict) and data.get("failure_reason"):
        return str(data["failure_reason"])
    error_type = _field(tool_result, "error_type")
    if error_type:
        return str(error_type)
    if _field(tool_result, "success") is False:
        return "not_verified_success"
    return "invalid_or_missing_tool_result"


# ─────────────────────────────────────────────
#  Bagian 5: verified-fact shape/metadata
# ─────────────────────────────────────────────

@dataclass
class VerifiedFact:
    entity_id: str
    value: Any
    verified: bool
    source: str
    tool_name: Optional[str]
    timestamp: str
    request_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────
#  Bagian 2/6/7: the store
# ─────────────────────────────────────────────

class VerifiedFactStore:
    """One fact per `entity_id` (Bagian 6 - conflict resolution: the
    newest verified state overwrites the old one in place, it is never
    appended as a second, contradicting entry). Thread-safe, in-memory,
    with the same lightweight JSON-file persistence pattern
    `luno.memory`'s own long-term store already uses - so verified
    facts survive a restart without adding a new storage dependency.

    This store contains ONLY verified facts - conversation history
    lives entirely in `luno.memory` and this class never reads or
    writes it - so Bagian 7's "verified fact selalu menang saat
    retrieval" is trivially true for anything read through here: there
    is nothing else in this store to lose to.
    """

    def __init__(self, path: Optional[str] = None, autosave: bool = True) -> None:
        self._lock = threading.Lock()
        self._facts: Dict[str, Dict[str, Any]] = {}
        # Verified Facts & Vision Memory Test Isolation sprint: reads
        # `config.VERIFIED_FACTS_FILE` (added that sprint) instead of
        # inlining `os.path.join(config.DATA_DIR, "verified_facts.json")`
        # directly - byte-identical default value, now independently
        # test-redirectable like every sibling `*_FILE` store.
        self._path = path or config.VERIFIED_FACTS_FILE
        self._autosave = autosave
        self._load()

    # -- Bagian 1+2+4+5+6+8: the only write path -------------------------

    def record(self, tool_result: Any, tool_name: Optional[str] = None,
               request_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Bagian 4 - the fact's `value` always comes from
        `ToolResult.data["actual_state"]` (falling back to
        `expected_state` only when a handler doesn't report
        `actual_state` at all) - never from parsing `.message`'s
        natural-language text. Returns the stored fact dict, or `None`
        if the Memory Guard blocked it (or there was nothing storable -
        e.g. no `entity_id` at all, such as `run_script`)."""
        if not should_store_verified_result(tool_result):
            _log(f"Decision: BLOCK\nReason: {_blocked_reason(tool_result)}")
            return None

        data = _field(tool_result, "data")
        entity_id = data.get("entity_id") if isinstance(data, dict) else None
        if not entity_id:
            # Verified success, but nothing with an entity_id to key a
            # fact on (e.g. run_script) - not blocked, just nothing this
            # store applies to.
            return None
        value = data.get("actual_state", data.get("expected_state"))

        fact = VerifiedFact(
            entity_id=entity_id, value=value, verified=True, source="tool_result",
            tool_name=tool_name, timestamp=_utcnow_iso(), request_id=request_id,
        ).to_dict()

        with self._lock:
            existed = entity_id in self._facts
            self._facts[entity_id] = fact
            if self._autosave:
                self._save_locked()

        decision = "UPDATE" if existed else "STORE"
        reason = "newer_verified_state" if existed else "verified_success"
        _log(f"Decision: {decision}\nReason: {reason} ({entity_id} = {value!r})")
        return fact

    # -- Bagian 7: retrieval ----------------------------------------------

    def get(self, entity_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            fact = self._facts.get(entity_id)
            return dict(fact) if fact else None

    def all_facts(self) -> List[Dict[str, Any]]:
        """Every verified fact, newest first."""
        with self._lock:
            facts = [dict(f) for f in self._facts.values()]
        facts.sort(key=lambda f: f.get("timestamp") or "", reverse=True)
        return facts

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        """Persistent State Hardening V2 sprint: now loaded via
        `luno.persistence.safe_load_json()`. Missing file -> `_facts`
        stays `{}` (set in `__init__`), unchanged. Non-dict root ->
        silently ignored, `_facts` stays `{}`, unchanged. Parse
        failure -> logged via this module's own `_log()` (same
        "[MemoryGuard] ..." convention as before), `_facts` stays `{}`."""
        existed = os.path.exists(self._path)
        data, source = persistence.safe_load_json(
            self._path, default=None, validate=lambda d: isinstance(d, dict),
        )
        if data is not None:
            self._facts = data
        elif existed and source == "default":
            _log(f"✗ failed to load {self._path}")

    def _save_locked(self) -> None:
        """Caller must already hold `self._lock`. Persistent State
        Hardening V2 sprint: now written via
        `luno.persistence.atomic_write_json()` - backup-before-write +
        temp-file + fsync + `os.replace()`, replacing the previous
        naive direct write."""
        try:
            persistence.atomic_write_json(self._path, self._facts)
        except Exception as ex:
            _log(f"✗ failed to save {self._path}: {ex}")
