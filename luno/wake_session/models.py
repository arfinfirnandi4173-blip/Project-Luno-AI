"""
models.py
=========

`ConversationState` - the closed set of session-level states from the
Sprint 2 spec, and `WakeSessionConfig` - every tunable, env-var-only
(never hardcoded) knob this package exposes.

`ConversationState` is DELIBERATELY separate from
`luno.behavior_tree.state_machine.LunoState` - they answer different
questions. `LunoState` tracks Luno's moment-to-moment ACTIVITY
(listening/thinking/talking) and lives inside the protected
`behavior_tree` package, untouched by this sprint. `ConversationState`
tracks whether a CONVERSATION SESSION is open at all - is Luno dormant
waiting for a wake word, or does it currently have the user's ongoing
attention - which is exactly the new concept this sprint adds. The two
state machines run side by side: a session can be LISTENING while
`LunoState` cycles through its own busy states underneath it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


class ConversationState(str, Enum):
    #: Wake-word gating is OFF (`sleep_enabled=False`) - every utterance
    #: is forwarded immediately, no wake word ever required. Also the
    #: state a freshly-constructed session starts in before its first
    #: `start()` call decides whether gating applies.
    IDLE = "idle"

    #: Gating is ON and dormant - only a matching wake word (at or above
    #: the configured confidence) is processed; everything else is
    #: ignored (or, if it looked like a failed wake attempt, rejected).
    SLEEPING = "sleeping"

    #: Transient: a valid wake word just landed, the acknowledgement
    #: phrase is being spoken. Exits automatically once that playback
    #: finishes (or is cancelled/fails - never gets stuck here).
    AWAKENING = "awakening"

    #: Awake, actively expecting the user's next words. Inactivity
    #: timeout is running.
    LISTENING = "listening"

    #: The user's utterance has been forwarded into the real pipeline
    #: (Planner/Tool Manager/OpenRouter) and a reply is in flight. No
    #: timeout runs here - Luno itself is the one taking the time.
    THINKING = "thinking"

    #: Luno's reply is being spoken through Fish Audio.
    SPEAKING = "speaking"

    #: Just finished speaking - awake, awaiting a possible follow-up.
    #: Functionally identical to LISTENING (same timeout bucket) but
    #: kept as its own named state per spec, and useful for the console
    #: to show "Luno just answered, still your turn" distinctly.
    WAITING_USER = "waiting_user"


#: States in which the inactivity timeout clock is actually running -
#: see ConversationSession.touch()/tick(). Deliberately excludes
#: AWAKENING/THINKING/SPEAKING: Luno's own processing/talking must never
#: by itself extend or shrink the session (spec: "Speaking by Luno alone
#: must not extend the session indefinitely").
TIMEOUT_ACTIVE_STATES = frozenset({ConversationState.LISTENING, ConversationState.WAITING_USER})


def _split_words(raw: str) -> List[str]:
    return [w.strip() for w in raw.split(",") if w.strip()]


#: Bug fix (wake word configuration loading): the value `from_env()`
#: falls back to ONLY when NEITHER `WAKE_WORDS` nor `WAKE_WORD` is set -
#: mirrors `luno/config.py`'s OWN `WAKE_WORD = os.getenv("WAKE_WORD",
#: "alexa")` default EXACTLY, same "read the same env var independently,
#: never import across that boundary" rule already used for
#: `RealFishAudioConfig.from_env()` (`luno/adapters/fish_audio_real.py`)
#: and for `interrupt_words`/`resume_words` below (which fall back to
#: `barge_in`'s own env vars). `from_env()` used to fall back to an
#: invented `["luno", "hey luno", "hi luno"]` list that had no grounding
#: anywhere else in the project and, critically, was never even CHECKED
#: against the project's own pre-existing `WAKE_WORD` convention - so a
#: deployment that only ever configured (or relied on the default of)
#: `WAKE_WORD` (read by `luno/main.py`'s real wake-word pipeline) saw
#: this package silently substitute a completely different, disconnected
#: default the moment it moved onto the new event-driven runtime. This
#: constant is used ONLY by `_resolve_wake_words()` (i.e. only reachable
#: through `from_env()`) - it deliberately does NOT change the bare
#: dataclass field default below, which stays the pre-existing
#: `["luno", "hey luno", "hi luno"]` test/library convenience value (see
#: that field's own comment for why those are different concerns).
_LEGACY_DEFAULT_WAKE_WORD = "alexa"


@dataclass
class WakeSessionConfig:
    """Every knob the spec calls out, env-var only, `from_env()` is the
    only supported way to build a non-default one - mirrors
    `OpenRouterConfig.from_env()`'s pattern exactly.

    Bug fix (wake word configuration loading) - precedence `from_env()`
    resolves `wake_words` with, highest to lowest:
        1. `WAKE_WORDS` env var (plural, comma-separated - this
           package's own newer, multi-alias-capable knob)
        2. `WAKE_WORD` env var (singular - the project's pre-existing,
           established knob; `luno/config.py` and `luno/main.py`'s real
           wake-word pipeline already read this)
        3. Built-in default - `[_LEGACY_DEFAULT_WAKE_WORD]`, i.e. the
           exact SAME default `luno/config.py` itself falls back to,
           not an invented one.
    A configured wake word (from either env var) is NEVER silently
    overwritten by the built-in default - see `_resolve_wake_words()`.
    `wake_words_source`/`wake_words_conflict_warning` record WHERE the
    final value came from, for startup/reload logging (never used for
    any behavioral decision - purely diagnostic). Both are only ever
    populated by `from_env()` - see their own field comments below for
    why the plain constructor doesn't set them."""

    #: NOTE: this bare-constructor default (`WakeSessionConfig()` with no
    #: args, or with only SOME fields overridden) is deliberately
    #: UNCHANGED by this bug fix and stays independent of
    #: `_LEGACY_DEFAULT_WAKE_WORD` - it answers a different question
    #: ("what's a reasonable value for direct/standalone construction,
    #: e.g. in tests that never touch the environment at all") than
    #: `from_env()` answers ("what did this deployment actually
    #: configure"). Every REAL deployment goes through `from_env()`,
    #: never this bare default - see that classmethod for the actual
    #: bug fix.
    wake_words: List[str] = field(default_factory=lambda: ["luno", "hey luno", "hi luno"])
    #: purely diagnostic - see class docstring. Always set by `from_env()`;
    #: a config built via the plain constructor (tests, etc.) defaults to
    #: this human-readable "not env-loaded" marker rather than claiming a
    #: source it didn't actually resolve through.
    wake_words_source: str = "constructor default (not loaded from environment)"
    wake_words_conflict_warning: Optional[str] = None
    session_timeout_s: float = 15.0
    wake_acknowledgement: str = "Yes?"
    wake_confidence: float = 0.6
    sleep_enabled: bool = True

    #: Bug fix (wake session / barge-in integration): a lightweight,
    #: independently-configured copy of `barge_in.models.BargeInConfig`'s
    #: own `interrupt_words`/`resume_words` defaults - NOT imported from
    #: that package (this project's own established rule: zero
    #: cross-package imports between `wake_session` and `barge_in`, see
    #: `barge_in/matcher.py`'s docstring for the same reasoning already
    #: applied to `normalize()`). This is used ONLY for one narrow
    #: purpose: while genuinely awake (LISTENING/WAITING_USER/IDLE),
    #: `SessionManagerModule` must NOT forward an utterance that is
    #: plainly an interrupt/resume phrase ("stop", "cancel", "resume", ...)
    #: onward as a brand-new conversational request - `BargeInModule`
    #: already owns and acts on that utterance independently via its own
    #: Event Bus route on the SAME raw `speech_recognized` event. Getting
    #: this list slightly out of sync with `BargeInConfig`'s is harmless
    #: (worst case: a stray interrupt-ish phrase is ALSO sent to the LLM
    #: as a literal message, exactly today's pre-fix behavior) - it is
    #: never used to decide whether to actually interrupt anything.
    interrupt_words: List[str] = field(default_factory=lambda: [
        "stop", "cancel", "pause", "wait", "hold on", "enough", "that's enough",
        "never mind", "nevermind", "actually",
        "batal", "sudah", "diam dulu", "tunggu", "sebentar",
    ])
    resume_words: List[str] = field(default_factory=lambda: [
        "resume", "continue", "lanjutkan", "lanjut",
    ])

    @staticmethod
    def _resolve_wake_words() -> Tuple[List[str], str, Optional[str]]:
        """Bug fix (wake word configuration loading) - the ONE place
        `wake_words`' value AND its source are decided together, so the
        two can never drift apart (source only ever describes what
        `from_env()` ACTUALLY did, not what config theoretically exists).
        Returns `(wake_words, source, conflict_warning)`. Never overwrites
        a genuinely configured value with the built-in default - the
        default is only ever reached when NEITHER env var is set at all."""
        raw_plural = os.getenv("WAKE_WORDS")
        raw_singular = os.getenv("WAKE_WORD")

        plural_words = _split_words(raw_plural) if raw_plural and raw_plural.strip() else None
        singular_words = [raw_singular.strip().lower()] if raw_singular and raw_singular.strip() else None

        warning: Optional[str] = None
        if plural_words and singular_words and plural_words != singular_words:
            warning = (
                "Wake word configuration conflict - two different environment "
                f"variables define different wake words: WAKE_WORDS={raw_plural!r} "
                f"-> {plural_words} vs WAKE_WORD={raw_singular!r} -> {singular_words}. "
                "Using WAKE_WORDS (higher priority - explicit multi-alias override)."
            )

        if plural_words:
            return plural_words, "WAKE_WORDS environment variable", warning
        if singular_words:
            return singular_words, "WAKE_WORD environment variable (legacy - shared with luno/config.py)", warning
        return (
            [_LEGACY_DEFAULT_WAKE_WORD],
            "built-in default (mirrors luno/config.py's own WAKE_WORD default - "
            "no WAKE_WORDS or WAKE_WORD environment variable is set)",
            warning,
        )

    @classmethod
    def from_env(cls) -> "WakeSessionConfig":
        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        wake_words, wake_words_source, wake_words_conflict_warning = cls._resolve_wake_words()

        # WAKE_SESSION_INTERRUPT_WORDS/_RESUME_WORDS take priority if set;
        # otherwise fall back to reading the SAME env var `barge_in` reads
        # (BARGE_IN_INTERRUPT_WORDS/BARGE_IN_RESUME_WORDS) so a deployment
        # only has to configure the list once in the common case - still
        # two independently-read env vars, not a cross-package import.
        raw_interrupt = os.getenv("WAKE_SESSION_INTERRUPT_WORDS") or os.getenv("BARGE_IN_INTERRUPT_WORDS")
        interrupt_words = _split_words(raw_interrupt) if raw_interrupt else list(cls.__dataclass_fields__["interrupt_words"].default_factory())
        raw_resume = os.getenv("WAKE_SESSION_RESUME_WORDS") or os.getenv("BARGE_IN_RESUME_WORDS")
        resume_words = _split_words(raw_resume) if raw_resume else list(cls.__dataclass_fields__["resume_words"].default_factory())

        return cls(
            wake_words=wake_words,
            wake_words_source=wake_words_source,
            wake_words_conflict_warning=wake_words_conflict_warning,
            session_timeout_s=_float("SESSION_TIMEOUT", 15.0),
            wake_acknowledgement=os.getenv("WAKE_ACKNOWLEDGEMENT", "Yes?"),
            wake_confidence=_float("WAKE_CONFIDENCE", 0.6),
            sleep_enabled=_bool("SLEEP_ENABLED", True),
            interrupt_words=interrupt_words,
            resume_words=resume_words,
        )
