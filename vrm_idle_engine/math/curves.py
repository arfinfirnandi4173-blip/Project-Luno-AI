"""
math/curves.py
===============

Easing / interpolation curves used any time the engine needs to move a value
from A to B *smoothly* instead of linearly or instantly - e.g. gaze
retargeting, state-machine blend weights, and random-event motion envelopes.

All functions here are pure and stateless: given a normalized `t` in [0, 1]
they return an eased `t` in [0, 1] (except `remap`/`clamp`, which are plain
numeric helpers). Composing "eased t" with `lerp(a, b, eased_t)` is the
standard pattern used throughout the codebase.
"""

from __future__ import annotations

import math


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp `value` into [lo, hi]."""
    return max(lo, min(hi, value))


def lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation between a and b."""
    return a + (b - a) * t


def remap(value: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    """Remap `value` from [in_min, in_max] into [out_min, out_max]."""
    if in_max - in_min == 0:
        return out_min
    ratio = (value - in_min) / (in_max - in_min)
    return out_min + ratio * (out_max - out_min)


def smoothstep(t: float) -> float:
    """Classic 3t^2 - 2t^3 smoothstep. Zero velocity at both endpoints."""
    t = clamp(t)
    return t * t * (3.0 - 2.0 * t)


def smootherstep(t: float) -> float:
    """Ken Perlin's improved smoothstep: 6t^5 - 15t^4 + 10t^3.
    Zero velocity *and* zero acceleration at both endpoints - even gentler
    than `smoothstep`, used for transitions that must feel completely inertial
    (e.g. state-machine emotion blending)."""
    t = clamp(t)
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def ease_in_quad(t: float) -> float:
    t = clamp(t)
    return t * t


def ease_out_quad(t: float) -> float:
    t = clamp(t)
    return 1.0 - (1.0 - t) * (1.0 - t)


def ease_in_out_quad(t: float) -> float:
    t = clamp(t)
    if t < 0.5:
        return 2.0 * t * t
    return 1.0 - pow(-2.0 * t + 2.0, 2) / 2.0


def ease_in_out_cubic(t: float) -> float:
    t = clamp(t)
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - pow(-2.0 * t + 2.0, 3) / 2.0


def ease_in_out_sine(t: float) -> float:
    t = clamp(t)
    return -(math.cos(math.pi * t) - 1.0) / 2.0


def cubic_bezier_1d(t: float, p0: float = 0.0, p1: float = 0.0, p2: float = 1.0, p3: float = 1.0) -> float:
    """
    Evaluate a 1D cubic Bezier curve (Bernstein form) at parameter t in
    [0, 1], given four scalar control points. This is *not* the CSS
    `cubic-bezier(x1,y1,x2,y2)` timing function (which requires inverting
    x(t)); it's a direct scalar Bezier, handy for building custom asymmetric
    envelopes (e.g. a random event's motion intensity over its lifetime).
    """
    t = clamp(t)
    mt = 1.0 - t
    return (
        (mt ** 3) * p0
        + 3.0 * (mt ** 2) * t * p1
        + 3.0 * mt * (t ** 2) * p2
        + (t ** 3) * p3
    )


def exponential_smoothing(current: float, target: float, speed: float, dt: float) -> float:
    """
    Frame-rate independent exponential ease towards `target`. `speed` is a
    "how many e-foldings per second" rate: higher = snappier. This is the
    workhorse used for gaze tracking, eye convergence, and any "current value
    chases a moving target" behaviour, because unlike a fixed-step lerp it
    stays correct even if the frame rate changes.
    """
    alpha = 1.0 - math.exp(-speed * dt)
    return current + (target - current) * alpha


def triangle_envelope(t: float) -> float:
    """Rises 0->1 over [0, 0.5] then falls 1->0 over [0.5, 1]. Used for
    blink curves and other "go there and come back" one-shot motions."""
    t = clamp(t)
    return 1.0 - abs(2.0 * t - 1.0)
