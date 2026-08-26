"""
vrm_idle_engine
===============

Procedural full-body idle animation engine for VRM avatars, driven entirely by
real-time algorithms (noise fields, oscillators, easing curves, layered
blending, and a probabilistic event system) and streamed live to any VMC
Protocol receiver (VNyan, Warudo, VSeeFace, Virtual Motion Capture, ...).

No animation clip is ever recorded or looped. Every bone rotation and blend
shape value sent out is *computed on the spot* for the current timestamp, so
the character never repeats itself exactly, even after running for hours.

Package layout
--------------
config/      Tunable parameters (dataclasses + JSON loader/saver).
math/        Pure-Python building blocks: noise, easing curves, quaternion math.
vmc/         VMC Protocol OSC address map + the network client.
avatar/      Bone name catalogue + the `Pose` data structure used to describe
             "where every bone should be right now".
animation/   The actual procedural behaviours ("layers"): breathing, weight
             shift, micro-motion, head/eye/blink controllers, fingers, facial
             idle, and the random-event system. Each layer outputs a partial
             `Pose` + blend shape dict; layers are additively composited.
controller/  The state machine (Idle/Talking/Happy/...) and the top-level
             `AnimationController` that ticks every layer once per frame,
             composites the final pose, and pushes it out over VMC.
utils/       Small cross-cutting helpers (logging, frame timing).

See README.md at the project root for the full architecture write-up and a
guide on extending the engine with new layers, events, or states.
"""

__version__ = "1.0.0"
