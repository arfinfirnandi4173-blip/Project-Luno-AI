"""
vmc/protocol.py
=================

VMC Protocol constants: OSC address strings and blend-shape name aliasing.

The VMC Protocol (https://protocol.vmc.info) is implemented identically by
VNyan, Warudo, VSeeFace and Virtual Motion Capture itself - they all listen
on a UDP port for the same OSC address space, so a single sender works
against all four with no per-app branching required.

Blend shape aliasing
---------------------
VRM0 and VRM1 use different *preset* blend shape clip names for the same
expression (e.g. VRM0's "Joy" became VRM1's "happy"). Because we don't know
in advance which VRM spec version the loaded model uses, every semantic
expression this engine wants to drive (blink, smile, ...) is sent under
*both* names - unknown blend shape names are simply ignored by receivers, so
this costs nothing and maximizes compatibility. Any custom/non-standard
blend shape name a specific character model happens to expose (extra brow or
cheek clips, for instance) can be added to `BLEND_ALIASES` without touching
any other file.
"""

from __future__ import annotations

from typing import Dict, Tuple

# --- OSC addresses (see https://protocol.vmc.info/marionette-spec) --------

ADDR_AVAILABLE = "/VMC/Ext/OK"
ADDR_TIME = "/VMC/Ext/T"
ADDR_ROOT_POS = "/VMC/Ext/Root/Pos"
ADDR_BONE_POS = "/VMC/Ext/Bone/Pos"
ADDR_BLEND_VAL = "/VMC/Ext/Blend/Val"
ADDR_BLEND_APPLY = "/VMC/Ext/Blend/Apply"

# --- Canonical semantic blend-shape name -> (VRM0 name, VRM1 name, ...) ---
# Add more entries here (and nothing else needs to change) to support a new
# expression or a model-specific custom clip.
BLEND_ALIASES: Dict[str, Tuple[str, ...]] = {
    "blink_left": ("Blink_L", "blinkLeft"),
    "blink_right": ("Blink_R", "blinkRight"),
    "blink": ("Blink", "blink"),  # some models use one combined blink clip
    "joy": ("Joy", "happy"),
    "angry": ("Angry", "angry"),
    "sorrow": ("Sorrow", "sad"),
    "fun": ("Fun", "relaxed"),
    "neutral": ("Neutral", "neutral"),
    "vowel_a": ("A", "aa"),
    "vowel_i": ("I", "ih"),
    "vowel_u": ("U", "ou"),
    "vowel_e": ("E", "ee"),
    "vowel_o": ("O", "oh"),
    "look_left": ("LookLeft", "lookLeft"),
    "look_right": ("LookRight", "lookRight"),
    "look_up": ("LookUp", "lookUp"),
    "look_down": ("LookDown", "lookDown"),
    # Common (non-standard, but widely used by community VRM models) extra
    # clips for the facial-idle layer. If your model names these
    # differently, just add the real name to the tuple.
    "brow_up": ("Brow_Up", "browUp", "BrowUp"),
    "brow_down": ("Brow_Down", "browDown", "BrowDown"),
    "cheek": ("Cheek", "cheek", "Fun"),
}


def resolve_blend_names(canonical_key: str) -> Tuple[str, ...]:
    """Return every concrete blend-shape clip name a canonical semantic key
    should be sent under. Unknown keys fall back to sending the key itself
    verbatim (so callers can always pass a raw model-specific name too)."""
    return BLEND_ALIASES.get(canonical_key, (canonical_key,))
