"""
test_incremental_speech_buffer.py
====================================

LLM Streaming -> Real-Time Speech Pipeline sprint - Phase 13, "BUFFER"
scenarios (9-19). Pure `luno.incremental_speech.IncrementalSpeechBuffer`
tests - no event bus, no adapter, no TTS/audio.

Run:
    python3 -m pytest tests/test_incremental_speech_buffer.py -q
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.incremental_speech import IncrementalSpeechBuffer  # noqa: E402


def _feed_all(buf, deltas):
    chunks = []
    for d in deltas:
        chunks.extend(buf.feed(d))
    chunks.extend(buf.flush_final())
    return chunks


# ============================================================================
# 9. tokens combine correctly
# ============================================================================

def test_9_tokens_combine_correctly():
    buf = IncrementalSpeechBuffer(request_id="r1")
    chunks = _feed_all(buf, ["Memory Luno", " menyimpan", " data", " berdasarkan", " konteks."])
    joined = " ".join(c.text for c in chunks)
    assert joined == "Memory Luno menyimpan data berdasarkan konteks."


# ============================================================================
# 10. sentence boundary flush
# ============================================================================

def test_10_sentence_boundary_flushes_as_soon_as_confirmed():
    buf = IncrementalSpeechBuffer(request_id="r2")
    c1 = buf.feed("Kalimat pertama selesai.")
    assert c1 == []  # still might be extended - not confirmed yet
    c2 = buf.feed(" Kalimat kedua dimulai")
    assert len(c2) == 1
    assert c2[0].text == "Kalimat pertama selesai."
    assert c2[0].is_final is False
    final = buf.flush_final()
    assert len(final) == 1
    assert "Kalimat kedua dimulai" in final[0].text
    assert final[0].is_final is True


# ============================================================================
# 11. paragraph flush
# ============================================================================

def test_11_paragraph_boundary_flushes_as_its_own_chunk():
    buf = IncrementalSpeechBuffer(request_id="r3")
    chunks = _feed_all(buf, ["Paragraf pertama isinya begini.\n\n", "Paragraf kedua isinya beda lagi topiknya di sini."])
    texts = [c.text for c in chunks]
    assert any("Paragraf pertama" in t for t in texts)
    assert any("Paragraf kedua" in t for t in texts)
    assert texts[0] != texts[-1]


# ============================================================================
# 12. comma flush only after threshold
# ============================================================================

def test_12_comma_not_used_as_boundary_for_a_short_open_clause():
    """A short clause containing a comma must NOT be force-split just
    because a comma exists - only once the still-open tail has grown
    past the configured threshold."""
    buf = IncrementalSpeechBuffer(request_id="r4", max_buffer_chars=220)
    chunks = buf.feed("Halo, apa kabar")
    assert chunks == [], "a short comma-containing clause should not be force-split"


def test_12b_comma_used_as_boundary_once_tail_is_long_enough():
    buf = IncrementalSpeechBuffer(request_id="r4b", max_buffer_chars=40)
    long_no_terminal_punct = "kata kata kata kata kata, kata kata kata kata kata kata kata kata"
    chunks = buf.feed(long_no_terminal_punct)
    assert len(chunks) >= 1, "a long open clause with a comma should force-flush at/near the comma"


# ============================================================================
# 13. max buffer threshold
# ============================================================================

def test_13_max_buffer_threshold_forces_a_flush_with_no_punctuation_at_all():
    buf = IncrementalSpeechBuffer(request_id="r5", max_buffer_chars=30)
    long_text = "supercalifragilisticexpialidocious " * 4  # no clause punctuation anywhere
    chunks = []
    for i in range(0, len(long_text), 8):
        chunks.extend(buf.feed(long_text[i:i + 8]))
    chunks.extend(buf.flush_final())
    joined = " ".join(c.text for c in chunks)
    assert joined.split().count("supercalifragilisticexpialidocious") == 4
    assert len(chunks) > 1, "a long punctuation-less run should have been force-split into multiple chunks"
    for c in chunks:
        assert "supercalifragilisticexpialidociou s" not in c.text  # never mid-word


# ============================================================================
# 14. final flush
# ============================================================================

def test_14_final_llm_response_always_flushes_remaining_buffer():
    buf = IncrementalSpeechBuffer(request_id="r6")
    buf.feed("Kalimat tanpa tanda baca penutup yang masih menggantung")
    final = buf.flush_final()
    assert len(final) == 1
    assert "menggantung" in final[0].text
    assert final[0].is_final is True


def test_14b_flush_final_is_idempotent():
    buf = IncrementalSpeechBuffer(request_id="r6b")
    buf.feed("Halo dunia sekali lagi untuk tes.")
    first = buf.flush_final()
    second = buf.flush_final()
    assert second == []
    assert len(first) >= 1


# ============================================================================
# 15. no empty chunks
# ============================================================================

def test_15_no_empty_chunks_ever_produced():
    for deltas in [[""], [" ", " "], ["\n\n\n"], ["https://example.com/only/a/link"], ["Oke."], []]:
        buf = IncrementalSpeechBuffer(request_id="r7")
        chunks = _feed_all(buf, deltas)
        for c in chunks:
            assert c.text.strip() != "", f"empty chunk for deltas {deltas!r}"


# ============================================================================
# 16. no 1-word spam
# ============================================================================

def test_16_no_1_2_word_chunk_spam():
    """Short sentences get merged with whatever settles NEXT (see
    `test_9`/the module's own docstring) rather than spoken as isolated
    1-2 word utterances. The one legitimate exception is the very LAST
    chunk of the whole reply: if the reply's last sentence happens to be
    short, there is nothing left to merge it WITH, and "final LLM output
    always flushes the remaining buffer, never drops content" (Phase 5,
    rule 6) outranks the anti-spam heuristic - so only NON-final chunks
    are held to the strict length check here."""
    buf = IncrementalSpeechBuffer(request_id="r8")
    chunks = _feed_all(buf, ["Iya. ", "Benar sekali, itu tepat. ", "Lanjut."])
    assert len(chunks) >= 2
    for c in chunks:
        if c.is_final:
            continue
        assert len(c.text.split()) > 2 or len(c.text) >= 12, f"suspiciously short standalone chunk: {c.text!r}"


# ============================================================================
# 17. mixed Indonesian/English
# ============================================================================

def test_17_mixed_indonesian_english_streamed():
    buf = IncrementalSpeechBuffer(request_id="r9")
    chunks = _feed_all(buf, ["Aku suka main game.", " I also like coding.", " Keduanya seru banget."])
    assert len(chunks) == 3
    assert "game" in chunks[0].text
    assert "coding" in chunks[1].text
    assert "seru" in chunks[2].text


# ============================================================================
# 18. markdown normalization uses existing system
# ============================================================================

def test_18_markdown_normalization_reuses_existing_normalizer():
    buf = IncrementalSpeechBuffer(request_id="r10")
    chunks = _feed_all(buf, ["**Note:** this is ", "*important* and here is a ", "`code snippet` for you."])
    joined = " ".join(c.text for c in chunks)
    assert "**" not in joined and "*" not in joined and "`" not in joined


# ============================================================================
# 19. URL/code handling uses existing normalizer
# ============================================================================

def test_19_url_and_code_block_use_existing_normalizer():
    buf = IncrementalSpeechBuffer(request_id="r11")
    chunks = _feed_all(buf, [
        "Lihat dokumentasinya di ", "[sini](https://example.com/docs) ",
        "atau jalankan ```pip install foo``` dulu.",
    ])
    joined = " ".join(c.text for c in chunks)
    assert "https://" not in joined
    assert "```" not in joined


# ============================================================================
# Contract sanity (SpeechChunk correlation fields, reused not reimplemented)
# ============================================================================

def test_chunk_id_deterministic_and_sequence_zero_based():
    buf = IncrementalSpeechBuffer(request_id="turn-42", conversation_id="conv-9")
    chunks = _feed_all(buf, ["Satu.", " Dua.", " Tiga."])
    assert [c.sequence for c in chunks] == list(range(len(chunks)))
    assert all(c.chunk_id == f"turn-42:chunk:{c.sequence}" for c in chunks)
    assert all(c.request_id == "turn-42" for c in chunks)
    assert all(c.conversation_id == "conv-9" for c in chunks)


def test_only_last_chunk_is_final():
    buf = IncrementalSpeechBuffer(request_id="r12")
    chunks = _feed_all(buf, ["Satu.", " Dua.", " Tiga."])
    assert [c.is_final for c in chunks] == [False] * (len(chunks) - 1) + [True]


def test_empty_stream_never_opens_and_produces_nothing():
    buf = IncrementalSpeechBuffer(request_id="r13")
    assert buf.opened is False
    assert buf.flush_final() == []
    assert buf.opened is False


def test_close_marker_is_empty_text_and_final():
    buf = IncrementalSpeechBuffer(request_id="r14")
    buf.feed("Halo dunia sekali lagi supaya cukup panjang.")
    buf.feed(" Kalimat kedua.")
    buf.flush_final()
    marker = buf.make_close_marker()
    assert marker.text == "" and marker.is_final is True
