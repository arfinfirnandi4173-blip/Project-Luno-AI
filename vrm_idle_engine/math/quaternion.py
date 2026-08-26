"""
math/quaternion.py
====================

Minimal quaternion math: Euler(degrees) -> quaternion conversion, Hamilton
product (for correctly *composing* two rotations, e.g. "gaze rotation" and
"micro-jitter rotation" applied to the same bone), and spherical linear
interpolation (slerp), used whenever the engine needs to blend two full
poses together (state-machine transitions, layer blending with weight < 1).

The VMC Protocol transmits bone orientation as a quaternion (x, y, z, w), so
this is the on-the-wire representation everywhere in the engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Quaternion:
    """Immutable quaternion (x, y, z, w) - Unity/VRM convention."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 1.0

    # -- constructors --------------------------------------------------

    @staticmethod
    def identity() -> "Quaternion":
        return Quaternion(0.0, 0.0, 0.0, 1.0)

    @staticmethod
    def from_euler_deg(pitch_x: float = 0.0, yaw_y: float = 0.0, roll_z: float = 0.0) -> "Quaternion":
        """
        Build a quaternion from Tait-Bryan angles given in degrees. Order of
        composition is intrinsic Z * Y * X (roll after yaw after pitch),
        which matches the convention used throughout the animation layers.
        For the small idle-animation angles used in this engine (a handful
        of degrees) the exact axis order has negligible visual impact, but a
        single consistent convention avoids surprises when layers compose.
        """
        x = math.radians(pitch_x) / 2.0
        y = math.radians(yaw_y) / 2.0
        z = math.radians(roll_z) / 2.0
        cx, sx = math.cos(x), math.sin(x)
        cy, sy = math.cos(y), math.sin(y)
        cz, sz = math.cos(z), math.sin(z)

        qw = cx * cy * cz + sx * sy * sz
        qx = sx * cy * cz - cx * sy * sz
        qy = cx * sy * cz + sx * cy * sz
        qz = cx * cy * sz - sx * sy * cz
        return Quaternion(qx, qy, qz, qw)

    # -- operations ------------------------------------------------------

    def normalized(self) -> "Quaternion":
        n = math.sqrt(self.x ** 2 + self.y ** 2 + self.z ** 2 + self.w ** 2)
        if n == 0:
            return Quaternion.identity()
        return Quaternion(self.x / n, self.y / n, self.z / n, self.w / n)

    def multiply(self, other: "Quaternion") -> "Quaternion":
        """Hamilton product `self * other` - applies `other`'s rotation
        first, then `self`'s (standard quaternion composition order)."""
        w = self.w * other.w - self.x * other.x - self.y * other.y - self.z * other.z
        x = self.w * other.x + self.x * other.w + self.y * other.z - self.z * other.y
        y = self.w * other.y - self.x * other.z + self.y * other.w + self.z * other.x
        z = self.w * other.z + self.x * other.y - self.y * other.x + self.z * other.w
        return Quaternion(x, y, z, w)

    def as_tuple(self) -> tuple:
        """Return (x, y, z, w) exactly as the VMC Protocol expects it."""
        return (self.x, self.y, self.z, self.w)

    def to_euler_deg(self) -> tuple:
        """
        Inverse of `from_euler_deg`: decompose this quaternion back into
        (pitch_x, yaw_y, roll_z) degrees, using the matching closed-form
        formula for the same Z*Y*X composition order. This is what
        `avatar/constraints.py` uses to clamp a composed rotation's angles
        per axis before it's sent out.

        Like any three-axis Euler extraction, this has a gimbal-lock
        singularity at yaw = +-90 deg, where pitch and roll become
        ambiguous (their individual values can't be recovered, only their
        sum/difference). This engine only ever calls this on idle-animation
        rotations - at most a few tens of degrees per axis, composed from a
        handful of small layers - which stays far away from that
        singularity in practice; the clamp at asin's input guards against
        the case blowing up numerically even if it's ever approached.
        """
        x, y, z, w = self.x, self.y, self.z, self.w

        sin_pitch_cos_yaw = 2.0 * (w * x + y * z)
        cos_pitch_cos_yaw = 1.0 - 2.0 * (x * x + y * y)
        pitch = math.atan2(sin_pitch_cos_yaw, cos_pitch_cos_yaw)

        sin_yaw = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
        yaw = math.asin(sin_yaw)

        sin_roll_cos_yaw = 2.0 * (w * z + x * y)
        cos_roll_cos_yaw = 1.0 - 2.0 * (y * y + z * z)
        roll = math.atan2(sin_roll_cos_yaw, cos_roll_cos_yaw)

        return (math.degrees(pitch), math.degrees(yaw), math.degrees(roll))


def slerp(a: Quaternion, b: Quaternion, t: float) -> Quaternion:
    """
    Spherical linear interpolation between two quaternions. Used by the
    layer compositor to blend a layer's contribution in/out smoothly when
    its weight changes (e.g. an emotion layer fading in) and by the state
    machine when cross-fading between two avatar states.
    """
    ax, ay, az, aw = a.x, a.y, a.z, a.w
    bx, by, bz, bw = b.x, b.y, b.z, b.w

    dot = ax * bx + ay * by + az * bz + aw * bw
    # Take the shorter path around the hypersphere.
    if dot < 0.0:
        bx, by, bz, bw = -bx, -by, -bz, -bw
        dot = -dot

    dot = min(1.0, max(-1.0, dot))

    if dot > 0.9995:
        # Nearly identical - linear interpolation is numerically safer here.
        result = Quaternion(
            ax + (bx - ax) * t,
            ay + (by - ay) * t,
            az + (bz - az) * t,
            aw + (bw - aw) * t,
        )
        return result.normalized()

    theta_0 = math.acos(dot)
    theta = theta_0 * t
    sin_theta = math.sin(theta)
    sin_theta_0 = math.sin(theta_0)

    s0 = math.cos(theta) - dot * sin_theta / sin_theta_0
    s1 = sin_theta / sin_theta_0

    return Quaternion(
        s0 * ax + s1 * bx,
        s0 * ay + s1 * by,
        s0 * az + s1 * bz,
        s0 * aw + s1 * bw,
    ).normalized()
