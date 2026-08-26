"""
numbers_en.py
==============

Rule-based English number-to-words, no LLM. Handles plain integers
(any size, via the standard short-scale ones/teens/tens/scale-word
algorithm), a leading minus sign ("negative"), and simple decimals
(read digit-by-digit after "point" - the common natural-speech
convention, e.g. "3.14" -> "three point one four").
"""

from __future__ import annotations

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
         "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
         "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SCALES = ["", "thousand", "million", "billion", "trillion"]

_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _three_digits_to_words(n: int) -> str:
    parts = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append(f"{_ONES[hundreds]} hundred")
    if rest:
        if rest < 20:
            parts.append(_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            parts.append(_TENS[tens] + (f"-{_ONES[ones]}" if ones else ""))
    return " ".join(parts)


def int_to_words_en(n: int) -> str:
    if n == 0:
        return "zero"
    negative = n < 0
    n = abs(n)

    groups = []
    while n > 0:
        n, rem = divmod(n, 1000)
        groups.append(rem)

    words = []
    for i in reversed(range(len(groups))):
        if groups[i] == 0:
            continue
        chunk = _three_digits_to_words(groups[i])
        scale = _SCALES[i] if i < len(_SCALES) else f"(10^{i * 3})"
        words.append(f"{chunk} {scale}".strip())

    result = " ".join(words)
    return ("negative " + result) if negative else result


def number_to_words_en(number_str: str) -> str:
    """Accepts a numeric literal as text (optionally signed, optionally
    with one decimal point) and returns its natural spoken English
    form. Falls back to returning the input unchanged if it doesn't
    look like a plain number (defensive - callers should already only
    call this on regex-matched numeric spans)."""
    s = number_str.strip()
    negative = s.startswith("-")
    if negative:
        s = s[1:]

    if "." in s:
        int_part, _, frac_part = s.partition(".")
        int_part = int_part or "0"
        try:
            int_words = int_to_words_en(int(int_part))
        except ValueError:
            return number_str
        frac_words = " ".join(_DIGIT_WORDS[int(d)] for d in frac_part if d.isdigit())
        result = f"{int_words} point {frac_words}".strip()
    else:
        try:
            result = int_to_words_en(int(s))
        except ValueError:
            return number_str

    return ("negative " + result) if negative else result
