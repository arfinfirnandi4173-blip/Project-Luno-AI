"""
Bersihin teks SEBELUM dikirim ke TTS (GPT-SoVITS) — supaya nggak dibacain aneh:

- URL/link dihapus total (dibacain huruf-per-huruf/simbol keras-keras itu nggak ada
  gunanya buat voice assistant, lagipula user nggak bisa "klik" sesuatu yang diucapkan).
- Pola GAGAP ala tsundere (mis. "W-whatever", "I-it's", "b-bukan") diganti jeda koma
  — soalnya TTS nggak ngerti hyphen tanpa spasi di situ maksudnya "gagap", dia baca
  apa adanya (bisa jadi aneh/kepotong). Dikenali dari: 1 huruf, lalu hyphen TANPA
  spasi, lalu kata yang huruf awalnya SAMA (case-insensitive) — pola paling khas
  buat gagap, beda dari kata majemuk kayak "well-known" (bukan pengulangan huruf).
  Pakai koma (bukan "...") karena ellipsis bikin jeda kelamaan/nge-lag di sebagian
  TTS — koma jedanya lebih singkat, lebih pas buat efek gagap cepat.
- Em-dash/en-dash (—/–), double-hyphen (--), dan hyphen berspasi (" - ") diganti
  koma, karena sebagian mesin TTS membacanya literal sebagai "minus" alih-alih jeda.
- Rapiin spasi ganda yang mungkin muncul akibat penghapusan di atas.

SENGAJA cuma dipakai buat teks yang DIUCAPKAN (dikirim ke speak()/TTS) — teks yang
ditampilkan di caption avatar atau log console tetap versi asli/lengkap, supaya info
kayak link (dan gaya gagap aslinya) tetap kebaca kalau user lagi liat layar.
"""

import re

_URL_RE = re.compile(r'https?://\S+|www\.\S+')
# Pola gagap: 1 huruf + hyphen TANPA spasi + kata yang diawali huruf sama (case-insensitive).
# \b penting: mencegah ini match ke kata majemuk kayak "well-known" (huruf sebelum
# hyphen di situ "l", bukan huruf tunggal berbatas kata, jadi \b nggak bakal cocok).
_STAMMER_RE = re.compile(r'\b([A-Za-z])-(?=\1)', re.IGNORECASE)
_DASH_RE = re.compile(r'\s*[—–]\s*')          # em-dash & en-dash (karakter unicode)
_DOUBLE_HYPHEN_RE = re.compile(r'\s*--\s*')    # double-hyphen dipakai sbg pengganti em-dash
# Hyphen BIASA yang dipakai sebagai jeda kalimat — dikenali dari ADA SPASI di kedua
# sisinya (mis. "Oke - lampu nyala"). Sengaja BEDA dari hyphen tanpa spasi (mis.
# "10-15", "well-known") yang TIDAK disentuh, karena itu bukan kasus yang dikeluhkan
# dan mengubahnya malah bisa ngerusak makna angka/kata majemuk.
_SPACED_HYPHEN_RE = re.compile(r'\s+-\s+')
_MULTI_SPACE_RE = re.compile(r'\s{2,}')


def clean_for_speech(text):
    """Return versi teks yang lebih enak DIBACAIN TTS. Aman dipanggil dengan teks
    kosong/None (return apa adanya)."""
    if not text:
        return text

    cleaned = _URL_RE.sub('', text)
    cleaned = _STAMMER_RE.sub(r'\1, ', cleaned)
    cleaned = _DASH_RE.sub(', ', cleaned)
    cleaned = _DOUBLE_HYPHEN_RE.sub(', ', cleaned)
    cleaned = _SPACED_HYPHEN_RE.sub(', ', cleaned)
    cleaned = _MULTI_SPACE_RE.sub(' ', cleaned)
    cleaned = cleaned.strip(" ,")

    return cleaned