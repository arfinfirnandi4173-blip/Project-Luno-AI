"""
test_text_normalizer.py
=========================

Standalone tests for `luno.text_normalizer` - rule-based only, no LLM,
no Event Bus, no adapters. Covers every Sprint 3 requirement for this
package: Markdown removal, bullet-to-pause conversion (never spoken as
"minus"), preserved mathematical minus, URLs, emoji, repeated
punctuation, code blocks, abbreviations, natural English and
Indonesian number reading (including ranges), and a stress pass over a
long, mixed-content message.

Run:
    python3 -m luno.text_normalizer.tests.test_text_normalizer
"""

from __future__ import annotations

import traceback
from typing import Callable, List, Tuple

from luno.text_normalizer import normalize_for_speech
from luno.text_normalizer.numbers_en import int_to_words_en, number_to_words_en
from luno.text_normalizer.numbers_id import int_to_words_id, number_to_words_id

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# Markdown cleanup
# ============================================================================

@scenario
def test_bold_and_italic_are_unwrapped():
    assert normalize_for_speech("This is **very** important") == "This is very important"
    assert normalize_for_speech("This is *very* important") == "This is very important"
    assert normalize_for_speech("This is __very__ important") == "This is very important"
    assert normalize_for_speech("This is _very_ important") == "This is very important"


@scenario
def test_strikethrough_is_unwrapped():
    assert normalize_for_speech("~~wrong~~ right") == "wrong right"


@scenario
def test_headers_are_removed():
    assert normalize_for_speech("# Big Title\nbody text") == "Big Title, body text"
    assert normalize_for_speech("### Smaller heading") == "Smaller heading"


@scenario
def test_inline_code_keeps_content_strips_backticks():
    assert normalize_for_speech("run `pip install luno` first") == "run pip install luno first"


@scenario
def test_fenced_code_block_becomes_a_spoken_cue():
    text = "Here you go:\n```python\nprint('hi')\n```\ndone"
    out = normalize_for_speech(text)
    assert "```" not in out
    assert "code snippet" in out
    assert "print(" not in out


@scenario
def test_markdown_link_keeps_label_drops_url():
    out = normalize_for_speech("see [the docs](https://example.com/docs) for more")
    assert out == "see the docs for more"
    assert "example.com" not in out


@scenario
def test_bare_url_is_removed():
    out = normalize_for_speech("visit https://example.com/page?x=1 now")
    assert "https://" not in out
    assert "example.com" not in out
    assert "visit" in out and "now" in out


@scenario
def test_www_url_is_removed():
    out = normalize_for_speech("check www.example.com today")
    assert "www.example.com" not in out


# ============================================================================
# Bullets vs. mathematical minus (the core disambiguation requirement)
# ============================================================================

@scenario
def test_bullet_marker_becomes_a_pause_not_minus():
    out = normalize_for_speech("Shopping list:\n- milk\n- eggs\n- bread")
    assert "minus" not in out.lower()
    assert "milk" in out and "eggs" in out and "bread" in out
    assert out.strip().startswith("Shopping list")


@scenario
def test_star_and_plus_bullets_also_become_pauses():
    out = normalize_for_speech("* first\n+ second")
    assert "minus" not in out.lower()
    assert "first" in out and "second" in out


@scenario
def test_mathematical_minus_is_preserved_and_spoken():
    out = normalize_for_speech("the temperature is -5 degrees")
    assert "negative five" in out
    assert "minus" not in out  # English reading uses "negative", not "minus"


@scenario
def test_negative_number_at_start_of_line_is_not_mistaken_for_a_bullet():
    out = normalize_for_speech("-5 degrees is very cold")
    assert out.startswith("negative five")


@scenario
def test_hyphenated_compound_word_is_left_alone():
    out = normalize_for_speech("a well-known fact")
    assert "well-known" in out


@scenario
def test_spaced_hyphen_used_as_a_pause_becomes_a_comma():
    out = normalize_for_speech("I think - actually I'm sure - this works")
    assert " - " not in out
    assert "," in out


# ============================================================================
# Emoji / repeated punctuation
# ============================================================================

@scenario
def test_emoji_is_removed():
    out = normalize_for_speech("great job! 🎉🔥 keep going 😀")
    assert "🎉" not in out and "🔥" not in out and "😀" not in out
    assert "great job" in out and "keep going" in out


@scenario
def test_repeated_exclamation_collapses():
    assert normalize_for_speech("wow!!!") == "wow!"


@scenario
def test_repeated_question_marks_collapse():
    assert normalize_for_speech("really???") == "really?"


@scenario
def test_mixed_bang_question_collapses_to_first():
    out = normalize_for_speech("what?!?!")
    assert out in ("what?", "what!")


@scenario
def test_repeated_dots_become_ellipsis():
    assert normalize_for_speech("wait........") == "wait..."


# ============================================================================
# Abbreviations
# ============================================================================

@scenario
def test_english_abbreviations_are_expanded():
    assert "Doctor Smith" in normalize_for_speech("Dr. Smith is here")
    assert "for example" in normalize_for_speech("bring supplies, e.g. water and food")
    assert "et cetera" in normalize_for_speech("apples, oranges, etc.")
    assert "versus" in normalize_for_speech("Luno vs. Alexa")


@scenario
def test_indonesian_abbreviations_are_expanded():
    assert "Yang terhormat" in normalize_for_speech("Yth. Bapak/Ibu")
    assert "dan lain-lain" in normalize_for_speech("buku, pensil, dll.")


# ============================================================================
# Numbers - English
# ============================================================================

@scenario
def test_int_to_words_en_basic():
    assert int_to_words_en(0) == "zero"
    assert int_to_words_en(15) == "fifteen"
    assert int_to_words_en(42) == "forty-two"
    assert int_to_words_en(100) == "one hundred"
    assert int_to_words_en(123) == "one hundred twenty-three"
    assert int_to_words_en(1000) == "one thousand"
    assert int_to_words_en(1_000_000) == "one million"
    assert int_to_words_en(-7) == "negative seven"


@scenario
def test_number_to_words_en_decimal():
    assert number_to_words_en("3.14") == "three point one four"


@scenario
def test_normalize_for_speech_reads_plain_numbers_english():
    out = normalize_for_speech("I have 3 apples and 21 oranges")
    assert "three apples" in out
    assert "twenty-one oranges" in out


@scenario
def test_number_range_reads_as_to_in_english():
    out = normalize_for_speech("10-15 people attended")
    assert "ten to fifteen people attended" == out
    assert "ten-fifteen" not in out


# ============================================================================
# Numbers - Indonesian
# ============================================================================

@scenario
def test_int_to_words_id_irregulars():
    assert int_to_words_id(10) == "sepuluh"
    assert int_to_words_id(11) == "sebelas"
    assert int_to_words_id(100) == "seratus"
    assert int_to_words_id(1000) == "seribu"
    assert int_to_words_id(1_000_000) == "satu juta"
    assert int_to_words_id(-3) == "minus tiga"


@scenario
def test_number_to_words_id_decimal():
    assert number_to_words_id("3.14") == "tiga koma satu empat"


@scenario
def test_normalize_for_speech_reads_plain_numbers_indonesian():
    out = normalize_for_speech("saya punya 3 apel dan 11 jeruk", language="id")
    assert "tiga apel" in out
    assert "sebelas jeruk" in out


@scenario
def test_number_range_reads_as_sampai_in_indonesian():
    out = normalize_for_speech("10-15 orang hadir", language="id")
    assert "sepuluh sampai lima belas orang hadir" == out


@scenario
def test_language_alias_bahasa_indonesia_is_recognized():
    out = normalize_for_speech("ada 10 orang", language="bahasa indonesia")
    assert "sepuluh orang" in out


# ============================================================================
# Whitespace / structural cleanup + edge cases
# ============================================================================

@scenario
def test_empty_and_none_text_pass_through_unchanged():
    assert normalize_for_speech("") == ""
    assert normalize_for_speech(None) is None


@scenario
def test_multiple_blank_lines_collapse():
    out = normalize_for_speech("line one\n\n\n\nline two")
    assert out == "line one, line two"


@scenario
def test_multi_space_collapses():
    out = normalize_for_speech("too      many     spaces")
    assert "  " not in out


# ============================================================================
# Stress: one long, mixed-content message exercising every rule at once
# ============================================================================

@scenario
def test_stress_long_mixed_content_message_does_not_crash_and_is_clean():
    text = (
        "# Weekly Report\n\n"
        "Hi **Dr. Smith**, here's the update 🎉 (see [full report](https://example.com/report?id=42)):\n"
        "- Revenue is up 15%, i.e. from $100 to $115.\n"
        "- Temperature dropped to -3 degrees overnight.\n"
        "- We onboarded 10-15 new clients this week!!!\n"
        "```python\nprint('done')\n```\n"
        "That's all for now, etc. Visit www.example.com for more???\n"
    )
    out = normalize_for_speech(text)
    assert "```" not in out
    assert "https://" not in out and "www.example.com" not in out
    assert "🎉" not in out
    assert "minus" not in out.lower()
    assert "negative three" in out
    assert "ten to fifteen" in out
    assert "Doctor Smith" in out
    assert "that is" in out  # from "i.e."
    assert "et cetera" in out
    assert "!!!" not in out
    assert "???" not in out
    assert len(out) > 0


@scenario
def test_stress_many_short_messages_all_normalize_without_error():
    samples = [
        "hello!!", "-5", "10-20", "**bold**", "`code`", "www.test.com",
        "😀😀😀", "Dr. Who?!?!", "a-b-c", "- item one\n- item two",
    ] * 50
    for s in samples:
        result = normalize_for_speech(s)
        assert isinstance(result, str)


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
