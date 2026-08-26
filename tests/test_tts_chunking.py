"""
test_tts_chunking.py
======================

TTS Chunk Queue & Cancellation sprint - dedicated test suite for the
CHUNK CONTRACT: `luno.speech_chunk.SpeechChunk`/`build_speech_chunks()`
wrapping already-segmented text (`luno.response_output.build_dual_response()`'s
`voice_chunks`/`voice_chunks_raw` - text segmentation itself, and its own
much larger test suite, live in `tests/test_response_output.py` Section
5/6 and are NOT duplicated here - this file is specifically about the
CORRELATION CONTRACT: chunk_id/request_id/conversation_id/sequence/
total/is_final).

No event bus, no adapter, no TTS/audio - pure functions only.

Run:
    python3 -m pytest tests/test_tts_chunking.py -q
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.response_output import build_dual_response  # noqa: E402
from luno.response_policy import ResponsePolicy  # noqa: E402
from luno.speech_chunk import SpeechChunk, build_speech_chunks  # noqa: E402


def _chunks_for(text, depth="normal", max_chunk_chars=220, request_id="req-1", conversation_id="conv-1"):
    dual = build_dual_response(text, ResponsePolicy(depth=depth, score=32), max_chunk_chars=max_chunk_chars)
    return build_speech_chunks(
        dual.voice_chunks, dual.voice_chunks_raw, request_id=request_id, conversation_id=conversation_id,
    ), dual


# ============================================================================
# 1. short response -> 1 chunk
# ============================================================================

def test_1_short_response_produces_one_chunk():
    chunks, _ = _chunks_for("Bisa.")
    assert len(chunks) == 1
    assert chunks[0].text == "Bisa."
    assert chunks[0].is_final is True


# ============================================================================
# 2. long response -> multiple chunks
# ============================================================================

def test_2_long_response_produces_multiple_chunks():
    text = " ".join(f"Modul ini mendukung fitur nomor {n}." for n in
                     ["satu", "dua", "tiga", "empat", "lima", "enam", "tujuh"])
    chunks, _ = _chunks_for(text)
    assert len(chunks) >= 5
    assert chunks[-1].is_final is True
    assert all(not c.is_final for c in chunks[:-1])


# ============================================================================
# 3. sentence boundaries preserved
# ============================================================================

def test_3_sentence_boundaries_preserved_not_cut_mid_sentence():
    text = (
        "Pompa aquascape kamu kemungkinan perlu dibersihkan. "
        "Kalau alirannya sudah melemah, matikan pompa dulu. "
        "Setelah itu baru bongkar bagian impeller dan bersihkan kotorannya."
    )
    chunks, _ = _chunks_for(text, max_chunk_chars=220)
    assert [c.text for c in chunks] == [
        "Pompa aquascape kamu kemungkinan perlu dibersihkan.",
        "Kalau alirannya sudah melemah, matikan pompa dulu.",
        "Setelah itu baru bongkar bagian impeller dan bersihkan kotorannya.",
    ]
    for c in chunks:
        assert c.text.strip().endswith((".", "!", "?"))


# ============================================================================
# 4. word boundary fallback (oversized sentence, no clause punctuation)
# ============================================================================

def test_4_word_boundary_fallback_never_cuts_mid_word():
    text = "supercalifragilisticexpialidocious " * 1 + "kata sambung tanpa koma sama sekali di kalimat ini yang cukup panjang"
    chunks, _ = _chunks_for(text, max_chunk_chars=30)
    assert len(chunks) > 1
    joined = " ".join(c.text for c in chunks)
    for word in ["supercalifragilisticexpialidocious", "kata", "sambung", "kalimat", "ini"]:
        assert word in joined
    for c in chunks:
        assert not c.text.startswith(" ") and not c.text.endswith(" ")


# ============================================================================
# 5. no empty chunks
# ============================================================================

def test_5_no_empty_chunks_ever_produced():
    for text in ["", "   ", "\n\n\n", "Oke.", "A. B. C. D. E."]:
        chunks, _ = _chunks_for(text)
        for c in chunks:
            assert c.text.strip() != "", f"empty chunk for input {text!r}"


# ============================================================================
# 6. punctuation preserved
# ============================================================================

def test_6_punctuation_preserved_in_chunk_text():
    text = "Apakah ini aman? Ya, ini aman! Bagus, terima kasih."
    chunks, _ = _chunks_for(text)
    assert [c.text for c in chunks] == ["Apakah ini aman?", "Ya, ini aman!", "Bagus, terima kasih."]


# ============================================================================
# 7. markdown/code/url normalization via EXISTING normalizer
# ============================================================================

def test_7_markdown_code_url_use_existing_normalizer():
    text = "**Note:** run `pip install foo` then see [docs](https://example.com/docs)."
    chunks, dual = _chunks_for(text)
    joined = " ".join(c.text for c in chunks)
    assert "**" not in joined and "`" not in joined and "https://" not in joined
    assert "pip install foo" in joined
    # chunk text is the SAME already-normalized text `voice_text` uses -
    # no second/duplicate normalizer was invoked
    assert joined == dual.voice_text


# ============================================================================
# 8. mixed Indonesian/English
# ============================================================================

def test_8_mixed_indonesian_english_text():
    text = "Aku suka main game. I also like coding. Keduanya seru banget."
    chunks, _ = _chunks_for(text)
    assert len(chunks) == 3
    assert "game" in chunks[0].text
    assert "coding" in chunks[1].text
    assert "seru" in chunks[2].text


# ============================================================================
# 9. no-punctuation text
# ============================================================================

def test_9_text_with_no_punctuation_still_chunks_safely():
    text = "tolong nyalakan lampu kamar sekarang juga"
    chunks, _ = _chunks_for(text)
    assert len(chunks) >= 1
    assert all(c.text.strip() for c in chunks)
    joined = " ".join(c.text for c in chunks)
    for word in ["nyalakan", "lampu", "kamar"]:
        assert word in joined


# ============================================================================
# 10. normalized-empty text (e.g. only a URL, which the normalizer strips)
# ============================================================================

def test_10_text_that_normalizes_to_empty_produces_no_chunks():
    chunks, dual = _chunks_for("https://example.com/only/a/link")
    assert dual.voice_text.strip() == "" or dual.voice_chunks == []
    assert chunks == []
    for c in chunks:
        assert c.text.strip() != ""  # vacuous if chunks == [], but documents the invariant


# ============================================================================
# SpeechChunk contract itself - correlation fields
# ============================================================================

def test_chunk_id_deterministic_and_correlated_to_request_id():
    chunks, _ = _chunks_for("Satu. Dua. Tiga.", request_id="turn-42")
    assert [c.chunk_id for c in chunks] == ["turn-42:chunk:0", "turn-42:chunk:1", "turn-42:chunk:2"]
    assert all(c.request_id == "turn-42" for c in chunks)


def test_conversation_id_preserved_on_every_chunk():
    chunks, _ = _chunks_for("Satu. Dua.", conversation_id="conv-99")
    assert all(c.conversation_id == "conv-99" for c in chunks)


def test_conversation_id_none_is_safely_preserved_as_none():
    chunks, _ = _chunks_for("Satu. Dua.", conversation_id=None)
    assert all(c.conversation_id is None for c in chunks)


def test_sequence_and_total_are_consistent_and_zero_based():
    chunks, _ = _chunks_for("Satu. Dua. Tiga. Empat.")
    assert [c.sequence for c in chunks] == [0, 1, 2, 3]
    assert all(c.total == 4 for c in chunks)


def test_is_final_true_only_on_last_chunk():
    chunks, _ = _chunks_for("Satu. Dua. Tiga.")
    assert [c.is_final for c in chunks] == [False, False, True]


def test_deterministic_chunk_order_across_repeated_calls():
    text = "Satu. Dua. Tiga. Empat. Lima."
    chunks_a, _ = _chunks_for(text, request_id="same-id")
    chunks_b, _ = _chunks_for(text, request_id="same-id")
    assert [c.to_dict() for c in chunks_a] == [c.to_dict() for c in chunks_b]


def test_to_dict_round_trips_all_fields():
    chunks, _ = _chunks_for("Halo dunia.", request_id="r1", conversation_id="c1")
    d = chunks[0].to_dict()
    assert set(d.keys()) == {"chunk_id", "request_id", "conversation_id", "sequence", "total", "raw_text", "text", "is_final"}
    assert d["chunk_id"] == "r1:chunk:0"
    assert d["request_id"] == "r1"
    assert d["conversation_id"] == "c1"


def test_raw_text_field_present_and_aligned_with_text():
    chunks, dual = _chunks_for("Satu kalimat. Dua kalimat.")
    assert len(chunks) == len(dual.voice_chunks_raw)
    for c, raw in zip(chunks, dual.voice_chunks_raw):
        assert c.raw_text == raw


def test_build_speech_chunks_empty_input_returns_empty_list():
    assert build_speech_chunks([], request_id="r1") == []
    assert build_speech_chunks([], [], request_id="r1", conversation_id="c1") == []


def test_build_speech_chunks_never_reimplements_segmentation():
    """Structural guard: `luno/speech_chunk.py` must not import
    `luno.text_normalizer` or duplicate any splitting/normalization logic
    - it only WRAPS text `response_output.py` already produced."""
    import luno.speech_chunk as mod
    import inspect
    src = inspect.getsource(mod)
    assert "text_normalizer" not in src
    assert "normalize_for_speech" not in src
    assert "def _split" not in src  # no local sentence-splitting helpers
