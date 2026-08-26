"""
Text Normalizer (Sprint 3)
============================

Rule-based (no LLM, ever) text cleanup applied right before a reply is
sent to Fish Audio for TTS. Standalone, dependency-free, independently
testable - imports nothing from any other `luno.*` package.

    rules.py        - every regex/lookup table, gathered in one place
    numbers_en.py     - English integer/decimal -> words
    numbers_id.py      - Indonesian integer/decimal -> words
    normalizer.py        - normalize_for_speech(text, language=...), the public entry point

Quick start
--------------
    from luno.text_normalizer import normalize_for_speech

    normalize_for_speech("**Sure!** Here's the plan:\\n- Open Chrome\\n- Search Unity")
    # -> "Sure! Here's the plan:, Open Chrome, Search Unity"

    normalize_for_speech("The temperature is -5 degrees, up from -10 yesterday.")
    # -> "The temperature is negative five degrees, up from negative ten yesterday."
    # (a mathematical minus is preserved and read correctly - never
    #  confused with a bullet marker, which this function also handles)

    normalize_for_speech("Suhu hari ini -3 derajat", language="indonesian")
    # -> "Suhu hari ini minus tiga derajat"

See `main_runtime_demo.py` for where this is actually wired in -
`BehaviorTreeModule._speak()` calls it on every reply before publishing
`AssistantResponse`, so Fish Audio only ever receives already-clean text.
"""

from .normalizer import normalize_for_speech
from .numbers_en import int_to_words_en, number_to_words_en
from .numbers_id import int_to_words_id, number_to_words_id

__all__ = [
    "normalize_for_speech",
    "int_to_words_en", "number_to_words_en",
    "int_to_words_id", "number_to_words_id",
]
