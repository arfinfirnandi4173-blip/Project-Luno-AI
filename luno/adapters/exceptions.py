"""
exceptions.py
=============

Exceptions the Adapter Layer raises on purpose - programming/config
errors, never a stand-in for a crashed adapter at runtime (those are
caught inside `BaseAdapter.on_event()`/`start()`, logged, turned into a
`SystemError` event, and reflected in adapter state/health - see
`base.py` and `manager.py`).
"""

from __future__ import annotations


class AdapterError(Exception):
    """Base class for every exception this package raises on purpose."""


class AdapterNotFoundError(AdapterError):
    """Raised when an operation names an adapter that isn't registered."""


class AdapterAlreadyRegisteredError(AdapterError):
    """Raised by `AdapterRegistry.register()`/`AdapterManager.register()`
    when the name is taken."""


class AdapterDisabledError(AdapterError):
    """Raised when an operation requires an adapter to be enabled (e.g.
    starting it) but its config says otherwise."""


class AdapterConfigError(AdapterError):
    """Raised for malformed adapter configuration (bad event mapping,
    missing required field, ...)."""
