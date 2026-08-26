"""
Animation layers: each one is an independently-ticking procedural behaviour
that contributes a partial ``Pose`` (bone rotations) and/or blend shape
values for the current frame. The ``LayeredAnimator`` in ``layer.py``
composites every active layer's output into the single final pose that gets
sent out over VMC.

Layers in this package:
    breathing        - chest/shoulder/neck/head motion driven by a breath cycle
    weight_shift      - hips/spine/shoulders/feet weight transfer sway
    micro_motion      - continuous low-amplitude noise-driven "aliveness"
    head              - gaze-driven head turning incl. occasional look-at-camera
    eyes              - saccades, wandering, tracking, focus targets
    blink             - blink timing incl. double/slow/sleepy variants
    fingers           - per-finger idle curl/open/shift
    facial            - slow blend shape drift (smile, brow, cheek)
    emotion           - state-driven expression blending
    random_events     - probabilistic one-shot procedural mini-animations
"""
