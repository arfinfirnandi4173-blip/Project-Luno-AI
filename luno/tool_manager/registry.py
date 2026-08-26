"""
registry.py
===========

The spec's "Registry" section - a thread-safe `name -> ToolHandler` map.
Per the spec, "The Planner should only communicate through the registry"
- in practice that means the eventual Planner integration talks to
`ToolManager` (which owns a registry internally), not to individual
handler classes directly; this module is what makes that indirection
possible and lets tools be added/removed/swapped at runtime.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from .handler import ToolHandler
from .utils import log


class ToolRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: Dict[str, ToolHandler] = {}

    def register(self, name: str, handler: ToolHandler) -> None:
        with self._lock:
            replacing = name in self._handlers
            self._handlers[name] = handler
        log(f"Registered tool '{name}' -> {type(handler).__name__}" + (" (replaced existing)" if replacing else ""))

    def unregister(self, name: str) -> bool:
        with self._lock:
            existed = self._handlers.pop(name, None) is not None
        if existed:
            log(f"Unregistered tool '{name}'")
        return existed

    def get(self, name: str) -> Optional[ToolHandler]:
        with self._lock:
            return self._handlers.get(name)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._handlers

    def list_tools(self) -> List[str]:
        with self._lock:
            return sorted(self._handlers.keys())
