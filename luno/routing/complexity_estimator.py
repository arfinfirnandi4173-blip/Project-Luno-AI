"""
complexity_estimator.py
=========================

`estimate_complexity(text, intents) -> (ComplexityLevel, score)` - a
cheap, deterministic heuristic scorer (no I/O, no LLM call - same
reasoning as `intent_classifier.py`: this decision has to be made
BEFORE any LLM is invoked). Considers message length, explicit
"reasoning-flavored" keywords (EN+ID), which intents `classify_intent()`
already found, and simple multi-step structure - matching the spec's
own "Complexity estimation should consider: reasoning steps, planning,
code generation, architecture, debugging, logical deduction, synthesis."
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .models import REASONING_INTENTS, ComplexityLevel, Intent

_HIGH_SIGNAL_WORDS = [
    "why", "explain", "jelaskan", "mengapa", "kenapa", "analyze", "analisa",
    "compare", "bandingkan", "architecture", "arsitektur", "design", "desain",
    "debug", "trace", "root cause", "strategy", "strategi", "optimize", "optimalkan",
    "algorithm", "algoritma", "refactor", "synthesize", "sintesis",
]

_EXTREME_SIGNAL_WORDS = [
    "design a system", "rancang sistem", "write a program", "buat program",
    "multi-step plan", "rencana bertahap", "debug this stack trace", "prove that",
    "buktikan", "derive", "turunkan rumus", "end to end", "from scratch",
    "dari nol", "entire codebase", "seluruh kode",
]

_MULTI_STEP_RE = re.compile(r"\b(first|then|after that|lalu|setelah itu|dan kemudian|langkah)\b")
_CONJUNCTION_SPLIT_RE = re.compile(r"\bdan\b|\band\b|,")


def _multi_step_signal(lower: str) -> float:
    if _MULTI_STEP_RE.search(lower):
        return 0.8
    # 3+ conjunction-joined clauses in one utterance looks like several
    # distinct asks bundled into one turn, even without explicit
    # "first/then" language.
    clauses = [c for c in _CONJUNCTION_SPLIT_RE.split(lower) if c.strip()]
    return 0.5 if len(clauses) >= 3 else 0.0


def estimate_complexity(text: str, intents: List[Intent]) -> Tuple[ComplexityLevel, float]:
    lower = (text or "").lower()
    words = lower.split()
    score = 0.0

    # length signal - long messages tend to carry more to reason about,
    # capped so a long but simple message never alone reaches HIGH.
    score += min(len(words) / 40.0, 1.0)

    if any(w in lower for w in _HIGH_SIGNAL_WORDS):
        score += 1.5
    if any(w in lower for w in _EXTREME_SIGNAL_WORDS):
        score += 2.5

    intent_set = set(intents)
    if Intent.CODING in intent_set:
        score += 1.2
    if intent_set & REASONING_INTENTS:
        score += 1.0

    score += _multi_step_signal(lower)

    if intent_set == {Intent.GENERAL_CHAT}:
        score -= 0.3

    score = max(0.0, score)
    if score >= 3.0:
        level = ComplexityLevel.EXTREME
    elif score >= 1.8:
        level = ComplexityLevel.HIGH
    elif score >= 0.8:
        level = ComplexityLevel.MEDIUM
    else:
        level = ComplexityLevel.LOW
    return level, score
