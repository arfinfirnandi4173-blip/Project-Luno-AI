"""
matcher.py
==========

Pure text matching - no Event Bus, no threading, no I/O - so it's
trivially unit-testable and reusable from both the real
`SessionManagerModule` and any future non-console front end.

Matching rule: normalize (lowercase, collapse whitespace, strip a small
set of leading/trailing punctuation) both the configured phrase and the
incoming utterance, then accept if the utterance STARTS WITH the
phrase as a whole-word prefix (so "luno" matches "luno" and
"luno, open chrome" but not "lunochrome" or "lunotics"). The first
configured phrase (checked longest-first, so "hey luno" wins over a
looser "luno" for an utterance like "hey luno open chrome") that
matches wins; the text after it (if any) is returned as `remainder` so
a combined "Luno, open chrome" utterance doesn't need to be repeated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

_PUNCT_RE = re.compile(r"[,.\!\?;:]+")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, replace ALL punctuation (not just at the edges - "Luno,
    open chrome" must normalize the same as "Luno open chrome" so the
    wake-phrase prefix match isn't broken by the comma the user
    naturally pauses on) with a space, then collapse whitespace."""
    text = text.lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


@dataclass
class WakeMatch:
    matched_phrase: str
    remainder: str


def match_wake_word(text: str, wake_words: List[str]) -> Optional[WakeMatch]:
    """Returns a `WakeMatch` if `text` begins with any configured wake
    phrase, else None. Purely textual - confidence gating is a separate
    step (see `SessionManagerModule`), so this function has no opinion
    on whether the match should ultimately be accepted."""
    if not text or not wake_words:
        return None
    norm_text = normalize(text)
    if not norm_text:
        return None

    # Longest phrase first so "hey luno" is preferred over a bare "luno"
    # when both are configured and the utterance could match either.
    candidates = sorted({normalize(w) for w in wake_words if w.strip()}, key=len, reverse=True)

    for phrase in candidates:
        if not phrase:
            continue
        if norm_text == phrase:
            return WakeMatch(matched_phrase=phrase, remainder="")
        prefix = phrase + " "
        if norm_text.startswith(prefix):
            return WakeMatch(matched_phrase=phrase, remainder=norm_text[len(prefix):].strip())
    return None


def looks_like_interrupt_or_resume(text: str, interrupt_words: List[str], resume_words: List[str]) -> bool:
    """Bug fix (wake session / barge-in integration): is `text` plainly
    an interrupt/resume phrase ("stop", "cancel", "resume", ...) rather
    than a genuine new request? `SessionManagerModule` uses this to
    avoid forwarding such an utterance onward as a brand-new
    conversational turn while genuinely awake (LISTENING/WAITING_USER/
    IDLE) - `BargeInModule` already owns and acts on the SAME raw
    `speech_recognized` event independently via its own Event Bus route,
    so there is nothing for THIS module to do with it either way. Pure
    substring matching, same normalize-then-compare shape as
    `match_wake_word` above - no Event Bus, no side effects."""
    if not text:
        return False
    norm_text = normalize(text)
    if not norm_text:
        return False
    for phrase in list(interrupt_words) + list(resume_words):
        p = normalize(phrase)
        if not p:
            continue
        if norm_text == p or f" {p} " in f" {norm_text} ":
            return True
    return False
