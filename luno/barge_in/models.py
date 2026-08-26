"""
models.py
=========

`SpeakingMode` - the four interruption policies Sprint 3 calls for -
and `BargeInConfig`, every word list/prompt this package uses, all
env-var only (never hardcoded deep in logic) and reloadable, matching
the exact pattern `wake_session.models.WakeSessionConfig` already
established.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class SpeakingMode(str, Enum):
    #: General conversation, storytelling, explanations, tutorials,
    #: reading documentation. Interrupt = immediately: stop TTS, cancel
    #: any pending LLM stream, return to Listening.
    FREE = "free"

    #: Fire-and-forget actions already dispatched (opening a browser,
    #: downloading, turning on lights, searching, uploads). Interrupt =
    #: speech stops, but the underlying task keeps running.
    SOFT = "soft"

    #: Dangerous/destructive actions (deleting files, erasing memory,
    #: factory reset, dangerous Home Assistant actions). Interrupt =
    #: pause and ask for confirmation before actually cancelling anything.
    CONFIRM = "confirm"

    #: Emergency announcements. Interrupt = speech may pause, but the
    #: underlying emergency monitoring/workflow is NEVER silently
    #: cancelled - only an explicit resume brings the speech back.
    CRITICAL = "critical"


def _split(raw: str) -> List[str]:
    return [w.strip() for w in raw.split(",") if w.strip()]


@dataclass
class BargeInConfig:
    #: Generic "the user wants to interrupt somehow" phrases - which of
    #: the four behaviors actually happens is decided by the CURRENT
    #: SpeakingMode, not by which specific word was used (see
    #: `manager.BargeInModule._handle_interrupt`).
    interrupt_words: List[str] = field(default_factory=lambda: [
        "stop", "cancel", "pause", "wait", "hold on", "enough", "that's enough",
        "batal", "sudah", "diam dulu", "tunggu", "sebentar",
    ])

    #: Explicit "continue" - not itself one of the spec's "Interrupt
    #: Commands" (those are all stop-like), but required by the Runtime
    #: Demo's own "type stop/pause/resume/cancel" testing list.
    resume_words: List[str] = field(default_factory=lambda: [
        "resume", "continue", "lanjutkan", "lanjut",
    ])

    #: Recognized only while CONFIRM is actively waiting for an answer.
    confirm_yes_words: List[str] = field(default_factory=lambda: [
        "yes", "yeah", "yep", "confirm", "iya", "ya", "benar",
    ])
    confirm_no_words: List[str] = field(default_factory=lambda: [
        "no", "nope", "don't", "dont", "tidak", "nggak", "jangan",
    ])

    confirm_prompt: str = "Do you want to cancel the operation?"

    #: Rule-based SpeakingMode classification keyword lists - matched as
    #: substrings against the lowercased text describing the turn (the
    #: user's request and/or the tool/action invoked). Never an LLM call.
    confirm_keywords: List[str] = field(default_factory=lambda: [
        "delete", "erase", "wipe", "factory reset", "reset", "remove", "format",
        "hapus", "reset pabrik",
    ])
    soft_keywords: List[str] = field(default_factory=lambda: [
        "open", "browser", "download", "turn on", "turn off", "light", "lights",
        "search", "upload", "spotify", "play music",
        "buka", "unduh", "nyalakan", "matikan", "cari",
    ])

    free_acknowledgements: List[str] = field(default_factory=lambda: ["Okay.", "Sure."])

    @classmethod
    def from_env(cls) -> "BargeInConfig":
        defaults = cls()

        def _list(name: str, default: List[str]) -> List[str]:
            raw = os.getenv(name)
            return _split(raw) if raw else default

        return cls(
            interrupt_words=_list("BARGE_IN_INTERRUPT_WORDS", defaults.interrupt_words),
            resume_words=_list("BARGE_IN_RESUME_WORDS", defaults.resume_words),
            confirm_yes_words=_list("BARGE_IN_CONFIRM_YES_WORDS", defaults.confirm_yes_words),
            confirm_no_words=_list("BARGE_IN_CONFIRM_NO_WORDS", defaults.confirm_no_words),
            confirm_prompt=os.getenv("BARGE_IN_CONFIRM_PROMPT", defaults.confirm_prompt),
            confirm_keywords=_list("BARGE_IN_CONFIRM_KEYWORDS", defaults.confirm_keywords),
            soft_keywords=_list("BARGE_IN_SOFT_KEYWORDS", defaults.soft_keywords),
            free_acknowledgements=_list("BARGE_IN_FREE_ACK", defaults.free_acknowledgements),
        )
