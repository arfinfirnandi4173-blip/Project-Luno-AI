"""
route.py
========

`PatrolRoute` - the explicit, immutable model Sprint 71 Phase 1 asks
for, plus `validate_route()` - the ONE place every safety/schema rule a
route must satisfy is enforced, called both when a route is loaded from
`config/camera_patrol_routes.json` (see `_load_routes()` in
`controller.py`) and, defensively, again right before a patrol actually
starts (so a route file edited by hand between load and use can never
slip an invalid route into a running patrol).

Safety invariant (Sprint 71 brief, verbatim): "`loop=true` tidak berarti
infinite loop tanpa batas. Patrol WAJIB memiliki salah satu batas
berikut: `max_cycles`, atau `max_duration_seconds`. Jika keduanya tidak
tersedia, patrol harus ditolak." A non-looping route (`loop=False`) is
already inherently bounded (it runs the preset list exactly once, then
completes), so this requirement is enforced ONLY when `loop=True` -
matching the brief's own reasoning ("`loop=true` tidak berarti...")
precisely, not a broader restriction than what was actually asked for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


class PatrolRouteError(ValueError):
    """Raised by `validate_route()` / `PatrolRoute.validated()` for any
    rule violation below. A plain `ValueError` subclass (not a new base)
    so any existing `except ValueError` still catches it - same
    backward-compatible-subclass pattern this project already used for
    `DashboardBindError(OSError)` (Sprint 71 - Dashboard Startup &
    Access Recovery)."""


#: Hard ceilings, not just "reasonable defaults" - a route requesting
#: something above these is rejected outright rather than silently
#: clamped, so a typo (e.g. `dwell_seconds: 10000` meant to be `10`)
#: fails loudly at validation time instead of quietly running a patrol
#: nobody intended. Generous enough to never constrain any real use.
MAX_DWELL_SECONDS = 3600.0          # 1 hour dwell at a single preset
MAX_CYCLES = 1000
MAX_DURATION_SECONDS = 24 * 3600.0  # 24 hours
MAX_PRESETS_PER_ROUTE = 50


@dataclass(frozen=True)
class PatrolRoute:
    """Sprint 71 Phase 1's own minimal model, field-for-field:

        PatrolRoute
            name
            presets[]
            dwell_seconds
            loop
            max_cycles

    `max_duration_seconds` is the second half of the Phase 1 safety
    invariant ("`max_cycles`, atau `max_duration_seconds`") - both are
    `Optional[...]`, but `validate_route()` requires at least one to be
    set whenever `loop=True`. Frozen (immutable) - a route, once
    validated, is never mutated in place; starting a NEW patrol with
    different parameters means constructing a NEW `PatrolRoute`, so a
    running patrol's own route can never be changed out from under it
    mid-run (see `controller.py`'s own "route is captured once per
    patrol run" note)."""

    name: str
    presets: List[str] = field(default_factory=list)
    dwell_seconds: float = 10.0
    loop: bool = False
    max_cycles: Optional[int] = None
    max_duration_seconds: Optional[float] = None
    #: Sprint 71 Phase 1's own diagram ends every route with "Kembali ke
    #: Home" - best-effort (never turns a successful patrol into a
    #: FAILED one - see `controller.py::_attempt_return_home`), and
    #: skipped entirely on STOPPED/FAILED (Phase 7: never issue another
    #: PTZ command after a failure or a stop request). Defaults True to
    #: match the brief's own worked example; a route can opt out.
    return_home: bool = True

    def to_public_dict(self) -> dict:
        """Metadata-only representation - safe to put in an Event
        payload or dashboard response (Phase 9's own "no credential, no
        frame" rule). A `PatrolRoute` never contains credentials/RTSP
        URLs/session data in the first place (see this module's own
        Phase 8 persistence-boundary note in `controller.py`), so this
        is a plain field dump, not a redaction step."""
        return {
            "name": self.name,
            "presets": list(self.presets),
            "dwell_seconds": self.dwell_seconds,
            "loop": self.loop,
            "max_cycles": self.max_cycles,
            "max_duration_seconds": self.max_duration_seconds,
            "return_home": self.return_home,
        }


def validate_route(route: PatrolRoute) -> None:
    """Raises `PatrolRouteError` with a human-readable reason on the
    FIRST rule violated (routes are small - a full multi-error report
    isn't worth the complexity a single-command voice/text interface
    could not usefully surface anyway). Returns silently if the route is
    valid. Pure, no I/O, no camera/network access - "unknown preset" in
    the sense of "this preset isn't saved on the camera right now" is
    NOT checkable here (the camera's own preset list is only knowable
    via a live `getPresets()` call - see `real_camera_ptz.py`'s own
    "HONEST LIMITATION" section) and is instead surfaced honestly at
    RUN time, the same way a manual `goto_preset` to an unrecognized
    name already fails today (Phase 7 - the patrol simply stops with a
    FAILED status reporting exactly that)."""
    if not (route.name or "").strip():
        raise PatrolRouteError("patrol route must have a non-empty name")

    if not route.presets:
        raise PatrolRouteError(f"patrol route '{route.name}' has no presets - a patrol needs at least one")

    if len(route.presets) > MAX_PRESETS_PER_ROUTE:
        raise PatrolRouteError(
            f"patrol route '{route.name}' has {len(route.presets)} presets, "
            f"more than the {MAX_PRESETS_PER_ROUTE} allowed"
        )

    seen = set()
    for preset in route.presets:
        cleaned = (preset or "").strip()
        if not cleaned:
            raise PatrolRouteError(f"patrol route '{route.name}' has an empty preset name")
        key = cleaned.lower()
        if key in seen:
            raise PatrolRouteError(
                f"patrol route '{route.name}' lists preset '{cleaned}' more than once - "
                "each preset may appear at most once per route"
            )
        seen.add(key)

    dwell = route.dwell_seconds
    if not isinstance(dwell, (int, float)) or isinstance(dwell, bool) or not math.isfinite(dwell):
        raise PatrolRouteError(f"patrol route '{route.name}' has an invalid dwell_seconds ({dwell!r})")
    if dwell < 0:
        raise PatrolRouteError(f"patrol route '{route.name}' has a negative dwell_seconds ({dwell})")
    if dwell > MAX_DWELL_SECONDS:
        raise PatrolRouteError(
            f"patrol route '{route.name}' has dwell_seconds={dwell}, more than the {MAX_DWELL_SECONDS}s allowed"
        )

    if route.max_cycles is not None:
        if not isinstance(route.max_cycles, int) or isinstance(route.max_cycles, bool) or route.max_cycles < 1:
            raise PatrolRouteError(f"patrol route '{route.name}' has an invalid max_cycles ({route.max_cycles!r})")
        if route.max_cycles > MAX_CYCLES:
            raise PatrolRouteError(
                f"patrol route '{route.name}' has max_cycles={route.max_cycles}, more than the {MAX_CYCLES} allowed"
            )

    if route.max_duration_seconds is not None:
        dur = route.max_duration_seconds
        if not isinstance(dur, (int, float)) or isinstance(dur, bool) or not math.isfinite(dur) or dur <= 0:
            raise PatrolRouteError(f"patrol route '{route.name}' has an invalid max_duration_seconds ({dur!r})")
        if dur > MAX_DURATION_SECONDS:
            raise PatrolRouteError(
                f"patrol route '{route.name}' has max_duration_seconds={dur}, "
                f"more than the {MAX_DURATION_SECONDS}s allowed"
            )

    # -- the Phase 1 safety invariant, verbatim ------------------------------
    if route.loop and route.max_cycles is None and route.max_duration_seconds is None:
        raise PatrolRouteError(
            f"patrol route '{route.name}' has loop=true but no bound (max_cycles or "
            "max_duration_seconds) - refusing to accept an unbounded patrol"
        )


def route_from_dict(name: str, data: dict) -> PatrolRoute:
    """Builds a `PatrolRoute` from one entry of `config/camera_patrol_
    routes.json` (see `controller.py::_load_routes()`). Does NOT
    validate - callers must call `validate_route()` themselves (kept
    separate so a route file can be loaded/inspected, e.g. by
    `get_status()`/tests, without necessarily being valid)."""
    presets = data.get("presets")
    if not isinstance(presets, list):
        presets = []
    return PatrolRoute(
        name=name,
        presets=[str(p) for p in presets],
        dwell_seconds=data.get("dwell_seconds", 10.0),
        loop=bool(data.get("loop", False)),
        max_cycles=data.get("max_cycles"),
        max_duration_seconds=data.get("max_duration_seconds"),
        return_home=bool(data.get("return_home", True)),
    )
