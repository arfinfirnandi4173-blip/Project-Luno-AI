"""
math/noise.py
=============

A from-scratch, dependency-free implementation of classic Perlin noise plus
fractal (fBm - fractional Brownian motion) summation, and a convenience
``NoiseField`` wrapper that hands out many decorrelated-but-smooth 1D noise
"channels" from a single generator.

Why noise instead of pure sine?
--------------------------------
A sine wave is perfectly periodic and perfectly predictable - two things a
living body never is. Perlin/fBm noise is continuous and smooth (C1
continuous, no popping) but never exactly repeats, which is exactly the
"never send an identical rotation twice" property the brief asks for. We use
it to (a) perturb sine-based oscillators (breathing, weight shift) so they
stop looking mechanical, and (b) drive micro-motion / finger idle directly,
where no rhythmic base signal is wanted at all.

Algorithm
---------
Standard 2D Perlin noise (Ken Perlin's improved/2002 formulation, simplified
to 2D): a 256-entry permutation table is shuffled once per seed, then for any
(x, y) we find the surrounding unit-square corners, hash each corner to a
pseudo-random gradient direction, take the dot product with the offset
vector, and bilinearly interpolate the four results using a quintic fade
curve (6t^5 - 15t^4 + 10t^3) so the interpolation itself has zero first and
second derivative discontinuities at cell boundaries.

fBm simply sums several "octaves" of this noise at increasing frequency and
decreasing amplitude, which adds fine detail on top of the broad shape -
this is what makes procedural noise look organic instead of like a single
smooth wobble.
"""

from __future__ import annotations

import math
import random
from typing import List


class PerlinNoise:
    """Seedable 2D Perlin noise generator, in [-1, 1]."""

    def __init__(self, seed: int = 0) -> None:
        rng = random.Random(seed)
        perm: List[int] = list(range(256))
        rng.shuffle(perm)
        # Duplicate the table so index+1 lookups never need to wrap/modulo.
        self._perm: List[int] = perm + perm

    @staticmethod
    def _fade(t: float) -> float:
        return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)

    @staticmethod
    def _lerp(t: float, a: float, b: float) -> float:
        return a + t * (b - a)

    @staticmethod
    def _grad(hash_: int, x: float, y: float) -> float:
        # 8 possible gradient directions, chosen by the low 3 bits of the hash.
        h = hash_ & 7
        u = x if h < 4 else y
        v = y if h < 4 else x
        return (u if (h & 1) == 0 else -u) + (v if (h & 2) == 0 else -v)

    def noise2(self, x: float, y: float) -> float:
        """Raw single-octave 2D Perlin noise, approximately in [-1, 1]."""
        xi = int(math.floor(x)) & 255
        yi = int(math.floor(y)) & 255
        xf = x - math.floor(x)
        yf = y - math.floor(y)

        u = self._fade(xf)
        v = self._fade(yf)

        perm = self._perm
        aa = perm[perm[xi] + yi]
        ab = perm[perm[xi] + yi + 1]
        ba = perm[perm[xi + 1] + yi]
        bb = perm[perm[xi + 1] + yi + 1]

        x1 = self._lerp(u, self._grad(aa, xf, yf), self._grad(ba, xf - 1.0, yf))
        x2 = self._lerp(u, self._grad(ab, xf, yf - 1.0), self._grad(bb, xf - 1.0, yf - 1.0))
        return self._lerp(v, x1, x2)

    def noise1(self, x: float) -> float:
        """1D convenience wrapper (fixes y = 0)."""
        return self.noise2(x, 0.0)

    def fbm(
        self,
        x: float,
        y: float = 0.0,
        octaves: int = 3,
        persistence: float = 0.5,
        lacunarity: float = 2.0,
    ) -> float:
        """Fractal Brownian motion: sum of several octaves of noise2."""
        total = 0.0
        amplitude = 1.0
        frequency = 1.0
        max_amplitude = 0.0
        for _ in range(max(1, octaves)):
            total += self.noise2(x * frequency, y * frequency) * amplitude
            max_amplitude += amplitude
            amplitude *= persistence
            frequency *= lacunarity
        return total / max_amplitude if max_amplitude > 0 else 0.0


class NoiseField:
    """
    Hands out many independent-looking, smoothly time-varying noise channels
    backed by a *single* Perlin generator instance.

    Each "channel" is simply a different Y-slice through the same 2D noise
    field; because Perlin noise decorrelates quickly along an axis, distinct
    integer-ish channel indices (spaced well apart) look statistically
    independent from one another while each individually stays perfectly
    smooth over time. This means every animation layer can request as many
    noise channels as it needs (one per bone, per finger, ...) without
    allocating a new generator each time.
    """

    def __init__(
        self,
        seed: int = 0,
        octaves: int = 3,
        persistence: float = 0.5,
        lacunarity: float = 2.0,
    ) -> None:
        self._perlin = PerlinNoise(seed)
        self.octaves = octaves
        self.persistence = persistence
        self.lacunarity = lacunarity

    def sample(self, channel: int, t: float, speed: float = 1.0) -> float:
        """Noise value in [-1, 1] for `channel` at time `t` (seconds)."""
        y = channel * 37.271828  # arbitrary large irrational-ish offset
        return self._perlin.fbm(t * speed, y, self.octaves, self.persistence, self.lacunarity)

    def sample01(self, channel: int, t: float, speed: float = 1.0) -> float:
        """Same as `sample` but remapped to [0, 1]."""
        return (self.sample(channel, t, speed) + 1.0) * 0.5

    def sample_range(self, channel: int, t: float, lo: float, hi: float, speed: float = 1.0) -> float:
        """Noise value remapped directly to the [lo, hi] range."""
        return lo + self.sample01(channel, t, speed) * (hi - lo)
