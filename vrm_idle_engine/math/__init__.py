"""
Pure-Python math building blocks used by every animation layer.

- ``noise``      : Perlin/fractal noise generator (deterministic, seedable).
- ``curves``      : Easing / interpolation curves (smoothstep, ease in-out, cubic bezier).
- ``quaternion``  : Minimal quaternion math (euler<->quaternion, slerp, multiply).

These modules have zero third-party dependencies on purpose: the whole engine
should be runnable with nothing but ``python-osc`` installed.
"""
