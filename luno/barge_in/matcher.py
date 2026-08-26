"""
matcher.py
==========

Pure text matching - no Event Bus, no threading. Deliberately does NOT
import `luno.wake_session` even though the normalization logic is
nearly identical - every package in this project stays independently
testable with zero cross-package imports (see `luno/core/__init__.py`'s
own stated design principle), so the ~10 lines of normalization are
duplicated here rather than shared.
"""

from __future__ import annotations

import re
from typing import List, Optional

_PUNCT_RE = re.compile(r"[,.\!\?;:]+")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _matches_any(norm_text: str, phrases: List[str]) -> bool:
    for phrase in phrases:
        p = normalize(phrase)
        if not p:
            continue
        if norm_text == p or f" {p} " in f" {norm_text} ":
            return True
    return False


def match_interrupt_word(text: str, interrupt_words: List[str]) -> bool:
    return _matches_any(normalize(text), interrupt_words)


def match_resume_word(text: str, resume_words: List[str]) -> bool:
    return _matches_any(normalize(text), resume_words)


def match_confirmation(text: str, yes_words: List[str], no_words: List[str]) -> Optional[bool]:
    """Returns True (confirmed/yes), False (declined/no), or None (not a
    recognizable answer - caller should keep waiting)."""
    norm = normalize(text)
    if _matches_any(norm, yes_words):
        return True
    if _matches_any(norm, no_words):
        return False
    return None
