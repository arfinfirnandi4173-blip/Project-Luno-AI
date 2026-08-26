"""
rules.py
=========

Every regex/lookup-table this package uses, gathered in one place so
`normalizer.py`'s pipeline reads as a short, ordered list of "apply
this rule" calls rather than a wall of inline regex. Rule-based only -
nothing here calls an LLM or any external service.
"""

from __future__ import annotations

import re

# -- code -------------------------------------------------------------------

#: Triple-backtick fenced blocks (with or without a language tag) - too
#: impractical to speak literally, so the whole block becomes a short
#: spoken cue instead of dead air or garbled symbol-reading.
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
CODE_BLOCK_REPLACEMENT = ", code snippet,"

#: Inline `code span` - strip the backticks, speak the content normally.
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# -- markdown ------------------------------------------------------------------

#: `[label](url)` -> just the label; the URL itself carries no spoken value.
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

#: **bold** / __bold__ - matched before the single-marker versions below
#: so a `**word**` pair isn't first partially consumed by the `*word*` rule.
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
#: *italic* / _italic_ - `(?<!\w)`/`(?!\w)` avoid mangling things like
#: `some_variable_name` where the underscores aren't emphasis markers.
ITALIC_RE = re.compile(r"(?<!\w)\*([^*\n]+)\*(?!\w)|(?<!\w)_([^_\n]+)_(?!\w)")
STRIKETHROUGH_RE = re.compile(r"~~([^~\n]+)~~")

#: `#`..`######` heading markers at the start of a line.
HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)

#: A markdown list bullet at the start of a line - "-", "*", or "+"
#: followed by at least one space. Requiring the trailing space is what
#: keeps this from ever matching a mathematical negative number like
#: "-5 degrees" (no space between the sign and the digit) or an inline
#: range like "10-15" (not at the start of a line at all) - both are
#: left completely untouched, exactly as the spec requires ("Preserve
#: mathematical minus").
BULLET_RE = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)

# -- links / urls ---------------------------------------------------------------

URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# -- emoji ------------------------------------------------------------------------

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF"
    "\U0001F000-\U0001F0FF"
    "️‍"
    "]+",
    flags=re.UNICODE,
)

# -- repeated punctuation --------------------------------------------------------

REPEATED_SAME_PUNCT_RE = re.compile(r"([!?,])\1+")
REPEATED_MIXED_BANG_QUESTION_RE = re.compile(r"[!?]{2,}")
REPEATED_DOTS_RE = re.compile(r"\.{2,}")

# -- whitespace -------------------------------------------------------------------

MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
MULTI_BLANK_LINE_RE = re.compile(r"\n{3,}")

# -- numbers ----------------------------------------------------------------------

#: A signed integer or simple one-decimal-point number, as its own
#: token (word boundary on the right so "10th"/"3rd" style ordinals and
#: things like "V2" aren't partially consumed - ordinal suffixes are a
#: documented out-of-scope simplification for this rule-based pass).
NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w)")

#: "10-15" (digit, hyphen, digit, no spaces) is a RANGE, not a negative
#: number - handled as its own rule, BEFORE `NUMBER_RE` runs, so the
#: hyphen becomes a spoken "to"/"sampai" instead of being left as a
#: bare, unconverted character glued between two number-words (which
#: is what happens if each side is converted independently and the
#: hyphen itself is never touched - exactly the "preserve mathematical
#: minus" requirement gone wrong if range hyphens are conflated with
#: negative-sign hyphens). Deliberately distinct from `NUMBER_RE`'s own
#: negative-sign handling, which only ever sees a hyphen with NO digit
#: immediately before it.
NUMBER_RANGE_RE = re.compile(r"(?<!\w)(\d+)-(\d+)(?!\w)")

# -- abbreviations ----------------------------------------------------------------

#: Ordered list, not a dict - order matters (e.g. multi-word phrases
#: like "e.g." must be checked before a hypothetical bare "eg" rule
#: would ever be added). Case-sensitive on purpose (an abbreviation
#: mid-sentence is normally written consistently); extend this list
#: freely, nothing else in the package needs to change.
ABBREVIATIONS = [
    (re.compile(r"\bDr\."), "Doctor"),
    (re.compile(r"\bMr\."), "Mister"),
    (re.compile(r"\bMrs\."), "Missus"),
    (re.compile(r"\bMs\."), "Miss"),
    (re.compile(r"\bProf\."), "Professor"),
    (re.compile(r"\bSt\."), "Street"),
    (re.compile(r"\be\.g\."), "for example"),
    (re.compile(r"\bi\.e\.", re.IGNORECASE), "that is"),
    (re.compile(r"\betc\.", re.IGNORECASE), "et cetera"),
    (re.compile(r"\bvs\.", re.IGNORECASE), "versus"),
    (re.compile(r"\bapprox\.", re.IGNORECASE), "approximately"),
    # Indonesian
    (re.compile(r"\bYth\."), "Yang terhormat"),
    (re.compile(r"\bdkk\."), "dan kawan-kawan"),
    (re.compile(r"\bdll\."), "dan lain-lain"),
    (re.compile(r"\btsb\."), "tersebut"),
    (re.compile(r"\bsbb\."), "sebagai berikut"),
    (re.compile(r"\byth\."), "yang terhormat"),
]
