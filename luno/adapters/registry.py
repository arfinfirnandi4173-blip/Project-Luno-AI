"""
registry.py
===========

`AdapterRegistry` - thread-safe bookkeeping of every adapter this
process knows about, enabled or not, independent of whether it's
currently registered with Core's `ModuleManager` (disabled adapters
stay here, visible for introspection/`enable()` later, but never reach
`ModuleManager` - see `manager.py`). Deliberately dumb storage only -
no lifecycle, no event routing, no dependency ordering; that's
`AdapterManager`'s job, built on top of this plus Core's own engine.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Dict, List, Optional

from .exceptions import AdapterAlreadyRegisteredError, AdapterNotFoundError
from .models import AdapterConfig

if TYPE_CHECKING:
    from .base import BaseAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._adapters: Dict[str, "BaseAdapter"] = {}
        self._configs: Dict[str, AdapterConfig] = {}

    def register(self, adapter: "BaseAdapter", config: AdapterConfig) -> None:
        with self._lock:
            if adapter.name in self._adapters:
                raise AdapterAlreadyRegisteredError(f"Adapter '{adapter.name}' already registered")
            self._adapters[adapter.name] = adapter
            self._configs[adapter.name] = config

    def unregister(self, name: str) -> bool:
        with self._lock:
            existed = name in self._adapters
            self._adapters.pop(name, None)
            self._configs.pop(name, None)
            return existed

    def get(self, name: str) -> Optional["BaseAdapter"]:
        with self._lock:
            return self._adapters.get(name)

    def require(self, name: str) -> "BaseAdapter":
        adapter = self.get(name)
        if adapter is None:
            raise AdapterNotFoundError(f"No adapter registered as '{name}'")
        return adapter

    def get_config(self, name: str) -> Optional[AdapterConfig]:
        with self._lock:
            return self._configs.get(name)

    def list_adapters(self) -> List[str]:
        with self._lock:
            return list(self._adapters.keys())

    def list_enabled(self) -> List[str]:
        with self._lock:
            return [n for n, cfg in self._configs.items() if cfg.enabled]

    def list_disabled(self) -> List[str]:
        with self._lock:
            return [n for n, cfg in self._configs.items() if not cfg.enabled]

    def all(self) -> Dict[str, "BaseAdapter"]:
        with self._lock:
            return dict(self._adapters)
