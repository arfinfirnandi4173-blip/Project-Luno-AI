"""
VMC Protocol layer: OSC address constants and the network client used to
push bone transforms and blend shape values to any VMC-compatible receiver
(VNyan, Warudo, VSeeFace, Virtual Motion Capture, ...).
"""
from vrm_idle_engine.vmc.client import VMCClient

__all__ = ["VMCClient"]
