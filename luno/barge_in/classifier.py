"""
classifier.py
==============

Rule-based (no LLM, ever) `SpeakingMode` classification. Pure function,
no Event Bus, no state - reusable and independently testable.

Classification order matters and is deliberate:
    1. `emergency_active` is a hard override to CRITICAL, regardless of
       what the text says - "Never silently cancel emergency workflows"
       means the emergency status always wins.
    2. Otherwise, keyword-match the descriptive text (the user's own
       request text and/or a description of the tool/action actually
       invoked for this turn) against the CONFIRM list (dangerous
       actions), then the SOFT list (fire-and-forget actions).
    3. Anything left over - plain conversation, explanations, questions
       with no matching tool - is FREE, the spec's own default.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from .matcher import normalize
from .models import BargeInConfig, SpeakingMode


def classify_speaking_mode(
    text: str = "",
    emergency_active: bool = False,
    config: Optional[BargeInConfig] = None,
) -> SpeakingMode:
    if emergency_active:
        return SpeakingMode.CRITICAL

    cfg = config or BargeInConfig()
    norm = normalize(text or "")
    if not norm:
        return SpeakingMode.FREE

    if _contains_any(norm, cfg.confirm_keywords):
        return SpeakingMode.CONFIRM
    if _contains_any(norm, cfg.soft_keywords):
        return SpeakingMode.SOFT
    return SpeakingMode.FREE


def _contains_any(norm_text: str, keywords: Iterable[str]) -> bool:
    for kw in keywords:
        if normalize(kw) and normalize(kw) in norm_text:
            return True
    return False
