"""
normalizer.py
==============

`normalize_for_speech(text, language=...)` - the single public entry
point. Rule-based only, no LLM call anywhere in this package. Order of
operations matters a great deal (see inline comments) - code blocks and
links must be unwrapped before anything else touches their contents,
bullets must be stripped before the number reader ever sees a bare
line-start "-", and abbreviation/number expansion must happen near the
end so earlier cleanup doesn't accidentally re-introduce something
those steps would need to re-process.
"""

from __future__ import annotations

import os

from . import rules
from .numbers_en import number_to_words_en
from .numbers_id import number_to_words_id

_ID_ALIASES = {"id", "indonesian", "bahasa", "bahasa indonesia", "in"}


def _normalize_language(language: str) -> str:
    return "id" if language.strip().lower() in _ID_ALIASES else "en"


def _convert_ranges(text: str, language: str) -> str:
    converter = number_to_words_id if language == "id" else number_to_words_en
    joiner = "sampai" if language == "id" else "to"

    def _sub(match):
        return f"{converter(match.group(1))} {joiner} {converter(match.group(2))}"

    return rules.NUMBER_RANGE_RE.sub(_sub, text)


def _convert_numbers(text: str, language: str) -> str:
    converter = number_to_words_id if language == "id" else number_to_words_en
    return rules.NUMBER_RE.sub(lambda m: converter(m.group(0)), text)


def _collapse_repeated_punctuation(text: str) -> str:
    text = rules.REPEATED_DOTS_RE.sub("...", text)
    text = rules.REPEATED_SAME_PUNCT_RE.sub(r"\1", text)
    # whatever's left of a run like "?!?!" - collapse to just its first character
    text = rules.REPEATED_MIXED_BANG_QUESTION_RE.sub(lambda m: m.group(0)[0], text)
    return text


def normalize_for_speech(text: str, language: "str | None" = None) -> str:
    """Clean `text` for TTS: strip Markdown/code/links/URLs/emoji,
    convert bullet markers into pauses (never spoken as "minus"),
    preserve and correctly vocalize genuine mathematical minus signs,
    collapse repeated punctuation, expand common abbreviations, and
    read plain numbers naturally in either English or Indonesian.
    Rule-based only - never calls an LLM. Safe to call with empty/None
    text (returns it unchanged)."""
    if not text:
        return text

    lang = _normalize_language(language if language is not None else os.getenv("LUNO_LANGUAGE", "english"))

    out = text

    # 1. Code - too impractical to speak literally.
    out = rules.CODE_BLOCK_RE.sub(rules.CODE_BLOCK_REPLACEMENT, out)
    out = rules.INLINE_CODE_RE.sub(r"\1", out)

    # 2. Links / URLs - the label (if any) is worth speaking, the URL never is.
    out = rules.MARKDOWN_LINK_RE.sub(r"\1", out)
    out = rules.URL_RE.sub("", out)

    # 3. Markdown emphasis - unwrap to plain text.
    out = rules.BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", out)
    out = rules.ITALIC_RE.sub(lambda m: m.group(1) or m.group(2) or "", out)
    out = rules.STRIKETHROUGH_RE.sub(r"\1", out)
    out = rules.HEADER_RE.sub("", out)

    # 4. Bullets - remove the marker (line-start only, always followed by
    #    a space - this is exactly what keeps a mathematical "-5" or an
    #    inline "10-15" from ever being touched here).
    out = rules.BULLET_RE.sub("", out)

    # 5. A hyphen used as a mid-sentence pause (spaces on both sides) -
    #    same reasoning as above: digit-adjacent hyphens never match
    #    this (no surrounding spaces), so math stays untouched.
    out = out.replace(" - ", ", ")

    # 6. Emoji - no spoken value, remove entirely.
    out = rules.EMOJI_RE.sub("", out)

    # 7. Repeated punctuation.
    out = _collapse_repeated_punctuation(out)

    # 8. Abbreviations.
    for pattern, replacement in rules.ABBREVIATIONS:
        out = pattern.sub(replacement, out)

    # 9. Numbers - ranges ("10-15") FIRST, so the range's own hyphen is
    #    consumed as a spoken "to"/"sampai" before the generic number
    #    rule below would otherwise convert each side independently and
    #    leave a bare, un-spoken hyphen glued between the two words.
    #    Runs after abbreviations/bullets/dashes so a genuine "-5" is
    #    the only kind of leading hyphen left to interpret as negative.
    out = _convert_ranges(out, lang)
    out = _convert_numbers(out, lang)

    # 10. Whitespace / line-break cleanup - every remaining line break
    #     becomes a short spoken pause so bullet lists (and anything
    #     else that was multi-line) read as one continuous, natural
    #     utterance instead of running words together.
    out = rules.MULTI_BLANK_LINE_RE.sub("\n", out)
    out = out.replace("\n", ", ")
    out = rules.MULTI_SPACE_RE.sub(" ", out)
    out = out.strip(" ,")

    return out
