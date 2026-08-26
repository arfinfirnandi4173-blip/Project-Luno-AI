"""
Preprocessing audio SEBELUM masuk ke Faster-Whisper — noise reduction + gain
normalization, buat ningkatin akurasi STT pas ngomong agak jauh dari mic atau di
ruangan yang ada noise latar (kipas, AC, dengung).

Desain penting: fungsi ini MENERIMA dan MENGEMBALIKAN bytes WAV (bukan numpy array
mentah) — supaya alur di transcribe_audio() (main.py) TETAP SAMA seperti sebelumnya
(tulis ke file sementara, kasih PATH-nya ke Faster-Whisper). Ini sengaja, karena
Faster-Whisper otomatis resample ke 16kHz sendiri kalau dikasih PATH FILE, tapi
TIDAK kalau dikasih array numpy mentah (diasumsikan udah 16kHz) — jadi cara ini
paling aman, nggak perlu implementasi resampling manual yang rawan bug.

Kalau preprocessing gagal di titik manapun (library nggak ada, audio kosong/aneh,
dll), fallback ke audio ASLI tanpa modifikasi — STT tetap jalan seperti biasa,
cuma nggak dapet peningkatan akurasinya buat giliran itu.
"""

import io
import numpy as np
import soundfile as sf

from . import config

try:
    import noisereduce as nr
    _NOISEREDUCE_AVAILABLE = True
except ImportError:
    _NOISEREDUCE_AVAILABLE = False
    print(
        "[STT] ⚠ Package 'noisereduce' belum terinstall — noise reduction dimatikan "
        "otomatis. Install dengan: pip install noisereduce\n"
    )


def preprocess_audio(wav_bytes):
    """Bersihin audio (noise reduction + normalize gain), return bytes WAV baru
    siap ditulis ke file & ditranskrip — SAMPLE RATE ASLI dipertahankan (nggak
    di-resample di sini), biar Faster-Whisper yang urus resampling-nya sendiri
    seperti biasa lewat file path."""
    if not config.STT_NOISE_REDUCE and not config.STT_NORMALIZE_GAIN:
        return wav_bytes  # dua-duanya dimatiin di .env -> skip semua, langsung balikin asli

    try:
        data, sample_rate = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)  # stereo -> mono (jaga-jaga kalau mic-nya stereo)

        if data.size == 0:
            return wav_bytes  # audio kosong, nggak ada yang bisa diproses

        if config.STT_NOISE_REDUCE and _NOISEREDUCE_AVAILABLE:
            data = nr.reduce_noise(y=data, sr=sample_rate, stationary=False)

        if config.STT_NORMALIZE_GAIN:
            peak = float(np.max(np.abs(data)))
            if peak > 1e-6:  # hindari divide-by-zero buat audio yang hening total
                data = data / peak * 0.95  # normalize ke 95% biar nggak clipping

        out_buf = io.BytesIO()
        sf.write(out_buf, data, sample_rate, format="WAV", subtype="PCM_16")
        return out_buf.getvalue()

    except Exception as ex:
        print(f"[STT] ✗ Preprocessing gagal, pakai audio asli: {ex}\n")
        return wav_bytes
