"""
voice_output_mode.py
=====================

Voice Output Mode sprint - the single source of truth for the ALL/SHORT
voice output mode enum, value validation, and the small set of explicit
command phrases that switch it at runtime.

Deliberately pure (no I/O, no event bus, no logging, no imports beyond
the standard library) - mirrors `luno.response_policy`'s and
`luno.response_output`'s own "deterministic, no side effects" module
contract (see those modules' own docstrings). Callers
(`main_runtime_demo.py`) own any logging when `resolve_voice_output_mode()`
falls back to the default, using the project's existing
`luno.core.utils.log()` - this module never imports it, so it stays
trivially unit-testable in isolation, same as `luno.response_policy`.

WHAT THE TWO MODES MEAN (see `luno.response_output.build_dual_response()`
for the actual implementation):

  - "ALL"   - the voice pipeline receives the reply's FULL sentence set.
              No near-duplicate dedup, no priority-budget selection, no
              orphan repair, no summarization. Text still goes through
              the EXISTING, mandatory `normalize_for_speech()` cleaning
              (markdown/code/links stripped, numbers spoken naturally) -
              that is not "compression" of WHICH content survives, it is
              the same TTS-legibility step every mode has always applied.
  - "SHORT" - the pre-existing, default depth-aware compression behavior
              (`_select_by_priority()` / `_repair_orphans()` / per-depth
              budgets) - completely UNCHANGED by this sprint. This is
              the compatibility/default path.

Deliberately a plain string enum (`"ALL"` / `"SHORT"`), never a bool -
this sprint's own explicit "extensible, more modes may exist later"
requirement. A third mode later means adding one more string here (plus
its own branch in `build_dual_response()`), never a second config
surface or a boolean flag.
"""

from __future__ import annotations

import re
from typing import Optional

#: The two modes this sprint defines.
VOICE_OUTPUT_MODE_ALL = "ALL"
VOICE_OUTPUT_MODE_SHORT = "SHORT"

#: The complete set of recognized modes today.
VOICE_OUTPUT_MODES = (VOICE_OUTPUT_MODE_SHORT, VOICE_OUTPUT_MODE_ALL)

#: Default stays SHORT so every caller that never touches this feature
#: at all sees byte-identical behavior to before this sprint (brief's
#: own explicit "default harus tetap SHORT" requirement).
DEFAULT_VOICE_OUTPUT_MODE = VOICE_OUTPUT_MODE_SHORT


def is_valid_voice_output_mode(value: object) -> bool:
    """True only for a string that normalizes (case/whitespace-
    insensitive) to one of `VOICE_OUTPUT_MODES`. Used by callers that
    want to log a warning specifically when a value was INVALID (as
    opposed to merely absent/`None`, which is the ordinary "use the
    default" case, not a warning-worthy one)."""
    return isinstance(value, str) and value.strip().upper() in VOICE_OUTPUT_MODES


def resolve_voice_output_mode(value: Optional[str]) -> str:
    """Normalizes `value` to one of `VOICE_OUTPUT_MODES`, or falls back
    to `DEFAULT_VOICE_OUTPUT_MODE` for anything unrecognized - `None`,
    empty/whitespace-only, a typo, a future/unknown mode name, or a
    non-string. Never raises - this is the one required "invalid value
    never crashes, always falls back to SHORT" guarantee from the
    brief's own Phase 1."""
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in VOICE_OUTPUT_MODES:
            return normalized
    return DEFAULT_VOICE_OUTPUT_MODE


# ─────────────────────────────────────────────
#  Explicit command phrases (Phase 5) - mirrors `luno.barge_in.matcher`'s
#  own normalize()/exact-or-whole-phrase matching style (re-implemented
#  here, not imported - this codebase's established convention, per that
#  module's own docstring, is that each small package stays
#  independently testable with zero cross-package imports for a ~10-line
#  normalizer).
# ─────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[,.\!\?;:]+")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _matches_any(norm_text: str, phrases) -> bool:
    for phrase in phrases:
        p = _normalize(phrase)
        if not p:
            continue
        if norm_text == p or f" {p} " in f" {norm_text} ":
            return True
    return False


#: Brief's own explicit example phrases (Phase 5) - deliberately a small,
#: fixed, bilingual phrase list, NOT a new classifier/intent model. A
#: phrase not in this list simply does not match (returns `None` from
#: `match_voice_output_mode_command()` below) - every existing turn's
#: behavior is completely unaffected, per Phase 5's own "prioritaskan
#: explicit command" + "jangan membuat classifier besar baru".
VOICE_MODE_ALL_PHRASES = (
    "voice semua", "baca semua", "bacakan semuanya", "bacakan semua",
    "read everything", "read it all", "voice all", "mode all", "read all",
)

VOICE_MODE_SHORT_PHRASES = (
    "mode short", "jawab singkat", "voice short", "jawab yang singkat",
    "mode singkat", "voice singkat", "read short",
)


def match_voice_output_mode_command(text: Optional[str]) -> Optional[str]:
    """Returns `VOICE_OUTPUT_MODE_ALL` / `VOICE_OUTPUT_MODE_SHORT` when
    `text` IS (exactly, or as a whole phrase within a short utterance -
    same "exact or ` phrase ` substring" rule `barge_in.matcher` already
    uses) one of the explicit command phrases above; `None` otherwise,
    meaning "not a mode command - leave mode untouched, let this turn
    proceed through the normal conversational/planning path unaffected."
    ALL is checked first purely because both lists are disjoint in
    practice (no phrase appears in both) - the order has no real
    effect."""
    if not text:
        return None
    norm = _normalize(text)
    if not norm:
        return None
    if _matches_any(norm, VOICE_MODE_ALL_PHRASES):
        return VOICE_OUTPUT_MODE_ALL
    if _matches_any(norm, VOICE_MODE_SHORT_PHRASES):
        return VOICE_OUTPUT_MODE_SHORT
    return None
