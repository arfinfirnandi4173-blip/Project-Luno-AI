"""
exceptions.py
=============

Every exception the Core Integration Layer raises on purpose. As with
every prior package this session, these are for *programming errors*
(bad registration, cyclic dependencies, misuse of the public API) -
runtime failures of a *module* (a crashing subsystem) are never allowed
to propagate as Python exceptions out of Runtime; they are caught,
logged, turned into a `SystemError` event, and reflected in module
state/health instead (see `module_manager.py`, `lifecycle.py`).
"""

from __future__ import annotations


class CoreError(Exception):
    """Base class for every exception this package raises on purpose."""


class ModuleNotFoundError(CoreError):
    """Raised when an operation names a module that isn't registered."""


class ModuleAlreadyRegisteredError(CoreError):
    """Raised by `ModuleManager.register()` when the name is taken."""


class DependencyCycleError(CoreError):
    """Raised when module dependencies form a cycle - startup order is
    undefined without a fix, so this is treated as a hard configuration
    error rather than something to route around silently."""


class ModuleStartError(CoreError):
    """Raised by `ModuleManager.start()`/`restart()` when the module's
    own `start()` raises. Startup as a whole does not stop because of
    this (see `lifecycle.py`); this exception is what a *direct* caller
    of `ModuleManager.start()` sees, distinct from `LifecycleManager`'s
    batch-startup behavior which catches it and continues."""


class ModuleStopError(CoreError):
    """Reserved for symmetry with `ModuleStartError`; `ModuleManager.stop()`
    currently swallows handler exceptions (a misbehaving `stop()` should
    never block shutdown of everything else) but raises this for
    programming-error cases, e.g. stopping a module still depended on by
    a running module."""


class EventBusError(CoreError):
    """Raised for Event Bus misuse - e.g. publishing something that isn't
    an `Event`."""


class ConfigError(CoreError):
    """Raised for configuration loading/parsing problems (missing
    dependency for a format, malformed file, etc.)."""
