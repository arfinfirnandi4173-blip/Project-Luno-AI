"""
numbers_id.py
==============

Rule-based Indonesian ("bahasa Indonesia") number-to-words, no LLM.
Handles the standard irregularities real Indonesian number reading
has: "sepuluh" (not "satu puluh") for 10, "sebelas" (not "satu belas")
for 11, "se-" prefixing for exactly one hundred ("seratus") and exactly
one thousand ("seribu") - but NOT for one million/billion/trillion,
which conventionally use "satu juta"/"satu milyar"/"satu triliun" in
regular counting, matching real usage. Negative numbers read "minus",
decimals read digit-by-digit after "koma" (e.g. "3,14" style speech:
"tiga koma satu empat").
"""

from __future__ import annotations

_ONES = ["nol", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan"]
_SCALES = ["", "ribu", "juta", "milyar", "triliun"]


def _three_digits_to_words_id(n: int) -> str:
    parts = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts.append("seratus" if hundreds == 1 else f"{_ONES[hundreds]} ratus")
    if rest:
        if rest == 10:
            parts.append("sepuluh")
        elif rest == 11:
            parts.append("sebelas")
        elif 12 <= rest <= 19:
            parts.append(f"{_ONES[rest - 10]} belas")
        elif rest < 10:
            parts.append(_ONES[rest])
        else:
            tens, ones = divmod(rest, 10)
            tens_word = f"{_ONES[tens]} puluh"
            parts.append(tens_word + (f" {_ONES[ones]}" if ones else ""))
    return " ".join(parts)


def int_to_words_id(n: int) -> str:
    if n == 0:
        return "nol"
    negative = n < 0
    n = abs(n)

    groups = []
    while n > 0:
        n, rem = divmod(n, 1000)
        groups.append(rem)

    words = []
    for i in reversed(range(len(groups))):
        g = groups[i]
        if g == 0:
            continue
        chunk = _three_digits_to_words_id(g)
        if i == 0:
            words.append(chunk)
        elif i == 1:
            words.append("seribu" if g == 1 else f"{chunk} ribu")
        else:
            scale = _SCALES[i] if i < len(_SCALES) else f"(10^{i * 3})"
            words.append(f"{chunk} {scale}")

    result = " ".join(words)
    return ("minus " + result) if negative else result


def number_to_words_id(number_str: str) -> str:
    """Same contract as `numbers_en.number_to_words_en` - accepts a
    numeric literal as text, returns its natural spoken Indonesian
    form, falls back to the original string if it doesn't parse."""
    s = number_str.strip()
    negative = s.startswith("-")
    if negative:
        s = s[1:]

    if "." in s:
        int_part, _, frac_part = s.partition(".")
        int_part = int_part or "0"
        try:
            int_words = int_to_words_id(int(int_part))
        except ValueError:
            return number_str
        frac_words = " ".join(_ONES[int(d)] for d in frac_part if d.isdigit())
        result = f"{int_words} koma {frac_words}".strip()
    else:
        try:
            result = int_to_words_id(int(s))
        except ValueError:
            return number_str

    return ("minus " + result) if negative else result
