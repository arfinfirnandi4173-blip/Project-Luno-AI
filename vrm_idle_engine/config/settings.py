"""
config/settings.py
===================

Every tunable number in the engine lives here, grouped into small
``@dataclass`` blocks (one per animation layer) and rolled up into a single
``EngineConfig``. This is the *only* file most users will ever need to touch
to reshape how the avatar moves.

Design rationale
----------------
- Dataclasses instead of a giant dict: editors get autocomplete + type
  checking, and `EngineConfig()` with no arguments already gives you a fully
  playable default configuration.
- ``load_config`` / ``save_config`` (de)serialize to plain JSON so the same
  parameters can be tweaked by a non-programmer (e.g. from a small GUI or a
  designer's text editor) without touching Python at all.
- Every sub-config is independent and self-contained: a new animation layer
  just adds one more dataclass here and one more field on ``EngineConfig``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, get_type_hints


# ---------------------------------------------------------------------------
# Per-layer configuration blocks
# ---------------------------------------------------------------------------

@dataclass
class NoiseConfig:
    """Global settings for the procedural noise field shared by many layers."""
    seed: int = 0
    # How many octaves of fractal (fBm) noise to sum. More octaves = more
    # organic-looking detail, at a (tiny) extra CPU cost.
    octaves: int = 3
    # Amplitude multiplier applied to each successive octave.
    persistence: float = 0.5
    # Frequency multiplier applied to each successive octave.
    lacunarity: float = 2.0


@dataclass
class BreathingConfig:
    """Procedural breathing: sine + noise driving chest/shoulders/neck/head."""
    speed: float = 0.22           # breath cycles per second (~4.5s per breath)
    chest_amplitude_deg: float = 2.2
    shoulder_amplitude_deg: float = 0.5
    neck_amplitude_deg: float = 0.4
    head_amplitude_deg: float = 0.3
    noise_influence: float = 0.35  # how much fBm noise perturbs the pure sine


@dataclass
class WeightShiftConfig:
    """Slow weight transfer between the two feet (hips/spine/shoulders/feet)."""
    speed: float = 0.08            # shifts per second (slow, ~12s per cycle)
    hip_yaw_deg: float = 2.5
    hip_roll_deg: float = 1.6
    spine_counter_ratio: float = 0.5   # torso counter-rotates vs hips (contrapposto)
    shoulder_counter_ratio: float = 0.3
    foot_lift_deg: float = 1.2         # subtle ankle/foot tilt following weight
    noise_influence: float = 0.4


@dataclass
class MicroMotionConfig:
    """Continuous low-amplitude noise-driven jitter across many joints."""
    global_amplitude_scale: float = 1.0
    spine_deg: float = 0.4
    hand_deg: float = 0.7
    knee_deg: float = 0.6
    ankle_deg: float = 0.5
    speed: float = 0.6              # base noise traversal speed
    # NOTE: finger micro-motion is owned by FingerIdleConfig/fingers.py (its
    # `wiggle_amplitude_deg`) so the same bones are never driven from two
    # independent config knobs at once.
    # When False, this layer only jitters the spine and skips hands/knees/
    # ankles entirely - what `EngineConfig.body_focus = "chest_up"` sets so
    # nothing below the chest ever gets a nonzero delta from this layer.
    include_lower_body: bool = True


@dataclass
class ArmIdleConfig:
    """Continuous idle sway for the upper arms/elbows - what keeps the arms
    from ever looking frozen once `RestPoseConfig` has brought them down out
    of the raw T-pose bind (see that dataclass's docstring)."""
    sway_amplitude_deg: float = 3.0      # side-to-side swing around the resting hang
    sway_speed: float = 0.35
    forward_amplitude_deg: float = 1.8   # slight forward/back drift
    elbow_amplitude_deg: float = 2.0     # tiny idle elbow bend
    elbow_speed: float = 0.3


@dataclass
class LegIdleConfig:
    """Continuous idle motion for the thighs and toes - the last two bones
    in the humanoid chain this engine leaves untouched otherwise (knees and
    ankles already get noise jitter from `MicroMotionConfig`). Kept very
    small: unlike arms, legs are load-bearing on a standing character, so
    anything beyond a subtle weight-settling wobble reads as broken instead
    of alive."""
    thigh_amplitude_deg: float = 1.0
    thigh_speed: float = 0.3
    toe_amplitude_deg: float = 2.5
    toe_speed: float = 0.4


@dataclass
class RestPoseConfig:
    """Default (pre-calibration) rest-pose corrections. VRM humanoid models
    are bound in T-pose or A-pose, where identity local rotation on
    `UpperArm` reproduces that raw bind orientation - i.e. arms stuck straight
    out to the sides. Rather than default to a visibly broken T-pose,
    `RestPose.with_natural_arm_defaults` bakes in a generic "bring the arms
    down to the sides" correction as the engine's default. If you calibrate
    a real model's bind pose via `RestPose.load_from_json`, that overrides
    this per-bone, so it's safe to leave as-is even for calibrated setups.

    Without a live model to test against, the correct axis/sign for "down"
    can only be confirmed empirically per model/rig - if arms swing the
    wrong way (up instead of down, or forward/back instead of sideways),
    try flipping the sign of `upper_arm_drop_deg` first, then try a
    different `upper_arm_drop_axis` ("roll_z", "pitch_x", or "yaw_y").
    `python tools/arm_tuner.py` runs a small utility that cycles through
    every combination automatically with the value printed to the console,
    so you can just watch the avatar and note which one looks right."""
    upper_arm_drop_deg: float = -75.0
    upper_arm_drop_axis: str = "roll_z"  # one of: "roll_z", "pitch_x", "yaw_y"


@dataclass
class HeadConfig:
    """Head look-around / gaze behaviour."""
    yaw_range_deg: float = 22.0
    pitch_range_deg: float = 10.0
    roll_influence: float = 0.4      # how much hip lean is mirrored as head tilt
    retarget_min_s: float = 2.0
    retarget_max_s: float = 5.0
    ease_speed: float = 2.2          # exponential smoothing rate (higher = snappier)
    look_at_camera_chance: float = 0.25  # probability a retarget looks near center
    # While `FrameContext.talking` is True and no explicit `gaze_override` is
    # set, the head stops its random idle retargeting and eases toward
    # facing forward instead - a character answering/speaking should read as
    # addressing the listener, not glancing around mid-sentence. An explicit
    # `gaze_override` (e.g. "look at the user's detected face") still wins
    # over this - it's a fallback default, not a hard lock.
    face_forward_when_talking: bool = True


@dataclass
class EyeConfig:
    """Eye saccade / wandering / tracking behaviour."""
    # Slower, calmer defaults than a real person's *scanning* saccades -
    # tuned to read as natural idle presence rather than nervous/robotic
    # darting. Real conversational saccades land more like every 1-3s; the
    # old 0.3-1.4s defaults were closer to actively reading text.
    saccade_min_s: float = 0.9
    saccade_max_s: float = 3.2
    saccade_range_deg: float = 8.0
    wander_range_deg: float = 5.0
    wander_speed: float = 0.3
    tracking_smooth_speed: float = 6.0   # how fast eyes converge on a focus target
    look_down_bias_deg: float = 1.0      # resting eyes tend slightly downward
    # How fast a picked saccade target is approached (see `eyes.py`'s
    # `_TRACKING_SMOOTH_FALLBACK`). Lower = more of a gentle glide, higher =
    # more of an instant "jump" (closer to a real saccade). Kept slightly
    # below the old hard-coded 12.0 so idle eye motion reads as calm rather
    # than twitchy while still snapping noticeably faster than the smooth
    # `tracking_smooth_speed` convergence used for an explicit focus target.
    saccade_snap_speed: float = 9.0
    # While `FrameContext.talking` is True, wander/saccade ranges are scaled
    # by this factor (not fully frozen - a totally static gaze reads as
    # dead/glassy) so the eyes stay close to center/forward instead of
    # drifting as widely as during idle silence.
    talking_range_scale: float = 0.4


@dataclass
class BlinkConfig:
    """Blink timing, including double/slow/sleepy variants."""
    min_interval_s: float = 2.2
    max_interval_s: float = 6.5
    close_duration_s: float = 0.09
    open_duration_s: float = 0.12
    double_blink_chance: float = 0.12
    slow_blink_chance: float = 0.10
    slow_blink_multiplier: float = 3.0
    sleepy_blink_chance: float = 0.05
    sleepy_hold_s: float = 0.35


@dataclass
class FingerIdleConfig:
    """Idle finger curl / open-close / shifting."""
    base_curl_deg: Dict[str, float] = field(default_factory=lambda: {
        "Proximal": -10.0, "Intermediate": -14.0, "Distal": -8.0,
    })
    wiggle_amplitude_deg: float = 2.5
    open_close_chance_per_min: float = 4.0   # avg number of open/close gestures/min
    open_close_amplitude_deg: float = 14.0
    open_close_duration_s: float = 1.1


@dataclass
class FacialIdleConfig:
    """Slow blend-shape drift: smile, brow, cheek."""
    smile_center: float = 0.12
    smile_amplitude: float = 0.10
    smile_speed: float = 0.05
    brow_amplitude: float = 0.06
    brow_speed: float = 0.07
    cheek_amplitude: float = 0.04
    cheek_speed: float = 0.045


@dataclass
class RandomEventConfig:
    """One-shot procedural mini-animations (hair touch, stretch, sigh, ...)."""
    min_interval_s: float = 10.0
    max_interval_s: float = 40.0
    enabled_events: tuple = (
        "hair_touch", "look_at_sky", "deep_breath", "stretch",
        "shift_feet", "cheek_touch", "play_with_hands", "head_tilt",
    )


@dataclass
class StateMachineConfig:
    """Blend transition speed between avatar states."""
    default_transition_s: float = 0.6
    sleepy_transition_s: float = 1.4


@dataclass
class VMCConfig:
    """Network settings for the VMC Protocol OSC client."""
    host: str = "127.0.0.1"
    port: int = 39539
    send_rate_hz: int = 60
    # Some receivers want /VMC/Ext/OK periodically to know the stream is alive.
    send_ok_heartbeat: bool = True
    ok_heartbeat_interval_s: float = 1.0


@dataclass
class EngineConfig:
    """Top-level configuration aggregating every layer's settings."""
    fps: int = 60
    seed: int = 0

    # "full_body" (default): every layer runs, including arms/legs/fingers/
    # weight-shift and the full random-event pool.
    # "chest_up": `AnimationController` skips `ArmIdleLayer`, `LegIdleLayer`,
    # `FingerIdleLayer` and `WeightShiftLayer` entirely (their bones just sit
    # at the calibrated rest pose - motionless, not T-pose), forces
    # `micro_motion.include_lower_body = False`, and restricts
    # `random_events.enabled_events` to the subset that never touches an arm/
    # hand/hip/leg bone (`deep_breath`, `look_at_sky`, `head_tilt`). Breathing,
    # head/eye/blink, facial idle and emotion posture are untouched either
    # way - they were already chest-and-up only. Use this for a half-body/
    # bust-only avatar setup, or any time hand/arm motion should stay off
    # while everything above the chest keeps moving.
    body_focus: str = "full_body"  # one of: "full_body", "chest_up"

    noise: NoiseConfig = field(default_factory=NoiseConfig)
    breathing: BreathingConfig = field(default_factory=BreathingConfig)
    weight_shift: WeightShiftConfig = field(default_factory=WeightShiftConfig)
    micro_motion: MicroMotionConfig = field(default_factory=MicroMotionConfig)
    arms: ArmIdleConfig = field(default_factory=ArmIdleConfig)
    legs: LegIdleConfig = field(default_factory=LegIdleConfig)
    rest_pose: RestPoseConfig = field(default_factory=RestPoseConfig)
    head: HeadConfig = field(default_factory=HeadConfig)
    eyes: EyeConfig = field(default_factory=EyeConfig)
    blink: BlinkConfig = field(default_factory=BlinkConfig)
    fingers: FingerIdleConfig = field(default_factory=FingerIdleConfig)
    facial: FacialIdleConfig = field(default_factory=FacialIdleConfig)
    random_events: RandomEventConfig = field(default_factory=RandomEventConfig)
    state_machine: StateMachineConfig = field(default_factory=StateMachineConfig)
    vmc: VMCConfig = field(default_factory=VMCConfig)


# ---------------------------------------------------------------------------
# (De)serialization helpers
# ---------------------------------------------------------------------------

def _dataclass_from_dict(cls, data: Dict[str, Any]):
    """Recursively build a dataclass instance from a plain dict, falling back
    to defaults for any missing keys (so partial config files are fine).

    Note: because this module uses ``from __future__ import annotations``,
    ``field.type`` on a dataclass field is a *string* (e.g. "NoiseConfig"),
    not the real class - so we resolve real types via
    ``typing.get_type_hints`` instead of reading ``field.type`` directly.
    """
    if not is_dataclass(cls):
        return data
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name in data:
            value = data[f.name]
            field_type = hints.get(f.name, f.type)
            if is_dataclass(field_type) and isinstance(value, dict):
                kwargs[f.name] = _dataclass_from_dict(field_type, value)
            else:
                kwargs[f.name] = value
    return cls(**kwargs)


def load_config(path: str | Path) -> EngineConfig:
    """Load an ``EngineConfig`` from a JSON file. Missing fields fall back to
    the dataclass defaults, so a config file only needs to list overrides."""
    path = Path(path)
    if not path.exists():
        return EngineConfig()
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    return _dataclass_from_dict(EngineConfig, data)


def save_config(config: EngineConfig, path: str | Path) -> None:
    """Serialize an ``EngineConfig`` to a human-editable JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(asdict(config), fp, indent=2, ensure_ascii=False)
