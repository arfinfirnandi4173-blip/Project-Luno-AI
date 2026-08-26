"""
animation/layer.py
====================

The animation-layer framework: an `AnimationLayer` is any procedural
behaviour that, given the current time and a shared `FrameContext`, produces
a partial `Pose`. A `LayeredAnimator` owns an ordered list of layers and
composites all of their output into one final `Pose` every tick.

This is intentionally the same mental model as a professional game's
animation-layer stack (base layer, additive layers, override layers): it's
what lets "breathing", "weight shift", "gaze", "blinking", "finger idle" and
"emotion" all run *simultaneously* and independently, instead of one giant
monolithic function that tries to compute the whole body at once - every
layer below runs every frame and all of them land in the *same* final pose
via `Pose.compose_prioritized`, which is what makes the result read as one
cohesive full-body animation rather than a pile of independent twitches.

Priority (`LayerPriority`)
----------------------------
Several layers legitimately touch the same bone (e.g. `ArmIdleLayer`'s
ambient sway and `RandomEventLayer`'s "stretch" gesture both rotate
`LeftUpperArm`). Rather than let whichever one happens to run last simply
pile its rotation on top, every layer declares a `priority` tier, and
`Pose.compose_prioritized` (called by `LayeredAnimator.tick`) uses it to
suppress lower-priority contributions to a bone/blend in proportion to how
active any higher-priority contributor on that *same* bone/blend currently
is. Concretely, from lowest to highest:

    AMBIENT (0)     breathing, weight_shift, micro_motion, arms
    DETAIL (10)     head, eyes, blink, fingers, facial
    EXPRESSION (20) emotion
    GESTURE (30)    random_events
    OVERRIDE (40)   talking

So a "stretch" random event (GESTURE) visibly dominates the ambient arm sway
(AMBIENT) on the bones it's actively using, without either fully silencing
the other or the two simply stacking into an exaggerated combined rotation
- and two layers in the *same* tier (e.g. breathing and weight_shift both
touching the shoulders) still combine exactly as before, since there's
nothing to suppress between peers.

Adding a new layer
-------------------
1. Subclass `AnimationLayer`, implement `update(self, context) -> Pose`.
2. Pick the `LayerPriority` tier that matches what kind of behaviour it is
   (see above) and pass it to `super().__init__(..., priority=...)`.
3. Instantiate it in `controller/animation_controller.py` and call
   `animator.add_layer(...)`.
That's it - nothing else in the engine needs to change, because the
compositor doesn't know or care how many layers exist or what bones they
touch, only their masks, weights, and priorities.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Iterable, List, Optional, Tuple

from vrm_idle_engine.avatar.pose import Pose
from vrm_idle_engine.math.noise import NoiseField


class LayerPriority(IntEnum):
    """Higher tiers dominate lower tiers wherever they touch the same
    bone/blend shape (see module docstring). Values are plain ints (not
    just an ordering) so a custom layer can slot in *between* tiers, e.g.
    `priority=15`, if that's ever genuinely needed."""
    AMBIENT = 0
    DETAIL = 10
    EXPRESSION = 20
    GESTURE = 30
    OVERRIDE = 40


@dataclass
class FrameContext:
    """
    Shared read-only(-ish) state handed to every layer on every tick.

    Attributes:
        t: Seconds since the engine started (monotonic, never resets).
        dt: Seconds since the previous tick (for frame-rate-independent
            integration, e.g. `exponential_smoothing`).
        noise: The shared `NoiseField`, so layers never have to instantiate
            their own Perlin generator - they just pick an unused channel
            index.
        rng: A shared `random.Random` instance (seeded, if a seed was
            configured) for anything that needs a one-shot random draw
            (blink variant selection, random-event choice, ...). Using one
            shared, optionally-seeded RNG instead of the global `random`
            module makes a run fully reproducible when a seed is set.
        state_weights: Current blend weight (0..1) of every `AvatarState`,
            updated once per tick by the state machine before layers run.
            The emotion layer reads this to mix facial expressions.
        gaze_override: If set, an externally-requested (yaw_deg, pitch_deg)
            the head/eye layers should prioritize over their own idle
            wandering - e.g. an AI assistant wanting the avatar to "look at
            the user" while talking.
        talking: Whether the talking layer should be actively driving mouth
            blend shapes this frame (set externally by whatever feeds text/
            audio into the engine; this engine only provides the hook).
    """

    t: float
    dt: float
    noise: NoiseField
    rng: random.Random
    state_weights: Dict[str, float] = field(default_factory=dict)
    gaze_override: Optional[Tuple[float, float]] = None
    talking: bool = False


class AnimationLayer(ABC):
    """
    Base class for every procedural animation behaviour.

    `bone_mask` / `blend_mask` are the **Layer Mask** mechanism: an explicit
    allow-list of bone names / blend-shape keys this layer is permitted to
    write. `None` (the default) means unrestricted. Setting a mask is
    optional but strongly recommended for every layer - it's a defensive
    guarantee, enforced centrally by `LayeredAnimator.tick` (not by trusting
    the layer's own `update()` implementation), that e.g. a bug in
    `BreathingLayer` can never reach out and rotate a finger bone, no matter
    what `update()` actually computes. Every built-in layer sets one; see
    each layer's `__init__`.
    """

    def __init__(
        self,
        name: str,
        weight: float = 1.0,
        bone_mask: Optional[Iterable[str]] = None,
        blend_mask: Optional[Iterable[str]] = None,
        priority: int = LayerPriority.AMBIENT,
    ) -> None:
        self.name = name
        # Mutable at runtime (e.g. the state machine fades `emotion`'s
        # weight in/out during a transition) - see `Pose.compose_prioritized`.
        self.weight = weight
        self.enabled = True
        self.bone_mask: Optional[frozenset] = frozenset(bone_mask) if bone_mask is not None else None
        self.blend_mask: Optional[frozenset] = frozenset(blend_mask) if blend_mask is not None else None
        self.priority = int(priority)

    @abstractmethod
    def update(self, context: FrameContext) -> Pose:
        """Compute this layer's contribution to the current frame."""
        raise NotImplementedError


class LayeredAnimator:
    """Owns and ticks an ordered stack of `AnimationLayer`s, applying each
    layer's mask and compositing the (masked) output into one final
    *animated-delta* `Pose` every frame. "Delta" because this is still
    relative to identity - turning it into the pose actually sent to VMC
    (rest-pose composition, skeleton validation, constraint clamping) is
    `avatar/pose_builder.py`'s `FinalPoseBuilder`'s job, not this class's."""

    def __init__(self) -> None:
        self._layers: List[AnimationLayer] = []

    def add_layer(self, layer: AnimationLayer) -> "LayeredAnimator":
        self._layers.append(layer)
        return self

    def get_layer(self, name: str) -> Optional[AnimationLayer]:
        for layer in self._layers:
            if layer.name == name:
                return layer
        return None

    @property
    def layers(self) -> List[AnimationLayer]:
        return list(self._layers)

    def tick(self, context: FrameContext) -> Pose:
        """Run every enabled layer for this frame, mask its output, and
        composite the result - priority-aware - into one delta pose."""
        entries: List[Tuple[Pose, float, int]] = []
        for layer in self._layers:
            if not layer.enabled or layer.weight <= 0.0:
                continue
            raw_pose = layer.update(context)
            masked_pose = raw_pose.filtered(layer.bone_mask, layer.blend_mask)
            entries.append((masked_pose, layer.weight, layer.priority))
        return Pose.compose_prioritized(entries)
