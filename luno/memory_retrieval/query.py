"""
query.py
========

Pure text analysis - no I/O, no imports from `vision_memory`/`planner`/
anything else. `analyze_query()` turns the user's raw utterance into a
`QueryAnalysis` that every registered source consumes identically.

This is the ONE place "keyword matching" lives today per the spec
("Keyword matching is acceptable initially. Architecture should allow
replacing it later with embeddings or vector search."). Swapping to a
real semantic/embedding strategy later means adding a new function here
(e.g. `analyze_query_semantic()`) and having `retriever.MemoryRetriever`
pick between them based on `MemoryRetrievalConfig.retrieval_mode` -
nothing about `RelevantMemory`, the source protocol, or the public
`retrieve_memories()` API needs to change for that swap.
"""

from __future__ import annotations

import re
from typing import List

from .models import QueryAnalysis

#: Memory Retrieval & Decision Quality (re-audit) sprint - Phase 0-2's own
#: live reproduction (`docs/change_impact/memory_retrieval_decision_quality.md`)
#: found this was the ORIGINAL pattern, `[a-zA-Z']+`, silently drops every
#: digit: "ESP32" tokenized to just "esp", "ESP8266" ALSO tokenized to just
#: "esp", "INMP441" tokenized to just "inmp" - three genuinely different
#: hardware identifiers collapsing onto one shared token everywhere this
#: tokenizer is reused (retrieval scoring, topic-term extraction, topic-
#: candidate content matching - all of it, by this module's own "one
#: tokenizer" design). That collision is what let turn 7 of the brief's own
#: 8-turn reproduction ("What did I use for the ESP32?") retrieve the
#: ESP8266/Bluetooth topic entry instead of the ESP32/mic one once the
#: correct entry aged out of the bounded topic history - a real, reproduced
#: cross-topic contamination, not a hypothetical.
#:
#: Fix: require a LEADING letter, then allow letters/digits/apostrophes -
#: keeps an alphanumeric identifier ("esp32", "inmp441", "esp8266") as ONE
#: whole token instead of truncating it, while a token that is ALL DIGITS
#: ("5", "441" on its own, "24" from "24/7") still never matches on its own,
#: exactly like before this fix - `test_3_empty_retrieval_for_no_signal_query`
#: ("What's 5 + 5?" -> no signal) stays true: "5" still produces no token
#: either way, only "what's" (already a stopword) tokenizes at all. Not a
#: second tokenizer - the same one regex, minimally widened.
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9']*")

#: words that carry no retrieval signal on their own - stripped before
#: keyword matching so "where IS my CUP" reduces to the one token that
#: actually matters ("cup"), same idea as a standard stopword list.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "am", "i", "my", "me", "you", "your",
    "it", "its", "this", "that", "these", "those", "do", "does", "did", "what", "where",
    "when", "who", "whose", "which", "how", "on", "in", "at", "of", "to", "for", "with",
    "and", "or", "but", "not", "no", "yes", "please", "can", "could", "would", "will",
    "there", "here", "so", "just", "about", "currently", "right", "now",
    # common contractions (the apostrophe-preserving _WORD_RE regex keeps
    # these as single tokens, e.g. "what's" - without listing them, a
    # purely mathematical/no-signal question like "what's 5 + 5?" would
    # wrongly register a retrieval signal from "what's" alone).
    "what's", "it's", "that's", "there's", "who's", "where's", "when's", "how's",
    "isn't", "aren't", "wasn't", "weren't", "don't", "doesn't", "didn't",
    "can't", "couldn't", "won't", "wouldn't", "shouldn't",
    "i'm", "i've", "i'll", "i'd", "you're", "you've", "we're", "they're",
}

#: phrases that mark a turn as being ABOUT THE USER themselves rather than
#: about an object/location in the room - checked against the NORMALIZED
#: (lowercased) text, not the stripped token set, since word order matters
#: here ("am i" vs a stray "i"/"am" elsewhere).
_SELF_QUERY_PATTERNS = [
    re.compile(r"\bam\s+i\b"),
    re.compile(r"\bwhat\s+am\s+i\b"),
    re.compile(r"\bhow\s+do\s+i\s+look\b"),
    re.compile(r"\bwhat\s+(?:do|does)\s+i\s+look\s+like\b"),
    re.compile(r"\bmy\s+(?:emotion|mood|activity|pose)\b"),
]

_TIME_REFERENCE_WORDS = {
    "yesterday", "today", "tonight", "morning", "afternoon", "evening", "earlier",
    "recently", "before", "ago", "minute", "minutes", "hour", "hours", "second",
    "seconds", "day", "days", "week", "weeks",
}

#: a query that reduces to ONLY these tokens (or nothing) has no
#: retrieval signal at all - e.g. "what's 5 + 5?" reduces to no word
#: tokens whatsoever after `_WORD_RE` strips the digits/punctuation, so
#: `has_any_signal` comes out False and sources should skip querying
#: their underlying store entirely (see spec: "Vision Memory should not
#: be queried unnecessarily").
_PURE_FILLER = _STOPWORDS


def analyze_query(user_text: str) -> QueryAnalysis:
    raw = user_text or ""
    normalized = raw.strip().lower()
    all_tokens = _WORD_RE.findall(normalized)
    signal_tokens = [t for t in all_tokens if t not in _STOPWORDS]

    is_self_query = any(p.search(normalized) for p in _SELF_QUERY_PATTERNS)
    mentions_time = any(t in _TIME_REFERENCE_WORDS for t in all_tokens)
    has_any_signal = bool(signal_tokens) or is_self_query

    return QueryAnalysis(
        raw_text=raw,
        normalized=normalized,
        tokens=signal_tokens,
        is_self_query=is_self_query,
        mentions_time=mentions_time,
        has_any_signal=has_any_signal,
    )


def token_overlap(tokens: List[str], text: str) -> bool:
    """True if ANY of `tokens` appears as a whole word inside `text`
    (case-insensitive). Used by the built-in vision sources to match a
    query token against an object's label or free-text location - e.g.
    tokens=["desk"] against location="on the wooden desk" -> True."""
    if not tokens or not text:
        return False
    text_words = set(_WORD_RE.findall(text.lower()))
    return any(t in text_words for t in tokens)
