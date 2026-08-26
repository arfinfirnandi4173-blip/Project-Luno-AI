"""
Bridge VMC ke VNyan — alternatif dari avatar_bridge.py (WebSocket+browser) buat yang
milih pakai software VNyan. VNyan sendiri yang urus render VRM, idle pose, physics
(pendulum/wobble), DAN lipsync (dari audio mic/virtual-cable). Modul ini CUMA kirim
EKSPRESI lewat protokol VMC STANDAR — semua gerakan/gesture lain (idle motion,
gesture "lagi ngejelasin" pas ngomong, dll) udah PINDAH ke vnyan_idle.py, yang juga
FULL VMC (bone rotation), BUKAN Trigger API/Node Graph lagi.

KENAPA CUMA VMC (nggak ada Trigger API lagi): sempat dicoba pakai VNyan Trigger API
buat trigger gesture custom, tapi ribet setup-nya (butuh REST API service VNyan
nyala + Node Graph khusus + exe terpisah) dan rawan putus di tengah jalan (connection
refused dkk). VMC (blendshape + bone rotation) udah kebukti jalan konsisten tanpa
dependency tambahan apa pun — jadi SEMUA kontrol avatar sekarang lewat situ aja.

SETUP DI SISI VNyan (sekali aja):
1. VNyan -> Settings -> OSC/VMC -> aktifkan "Receive VMC data", catat port-nya
   (samain sama VNYAN_OSC_PORT di .env, default 39539).
2. SELESAI — nggak perlu node-graph/plugin tambahan apa pun.

Format pesan VMC buat blendshape (standar resmi protokolnya):
  /VMC/Ext/Blend/Val (string){nama_blendshape} (float){nilai 0.0-1.0}
  ... (diulang buat tiap blendshape yang mau diubah)
  /VMC/Ext/Blend/Apply   <- WAJIB dikirim di akhir, baru semua perubahan diterapkan

CATATAN JUJUR: saya nggak punya VNyan buat test langsung — format pesan VMC-nya
udah sesuai spesifikasi resmi, tapi kecocokan PERSIS sama versi VNyan kamu tetap
perlu dicoba langsung di mesin kamu.
"""

from pythonosc import udp_client

from . import config

_client = None

# Tiap tag ekspresi internal Luno -> daftar nama blendshape yang mau di-set ke 1.0.
# Dikirim 2 gaya penamaan (VRM0 & VRM1) sekaligus biar aman apa pun versi model kamu.
_EXPRESSION_TO_BLENDSHAPES = {
    "happy": ["Joy", "happy"],
    "laughing": ["Joy", "happy"],
    "sad": ["Sorrow", "sad"],
    "angry": ["Angry", "angry"],
    "surprised": ["Surprised", "surprised"],
    "thinking": ["Fun", "relaxed"],
    "neutral": ["Neutral", "neutral"],
}

# Semua nama blendshape yang PERNAH dipakai di atas — dipakai buat reset ke 0.0
# nama-nama yang lagi TIDAK aktif tiap kali ganti ekspresi.
_ALL_BLENDSHAPE_NAMES = sorted({name for names in _EXPRESSION_TO_BLENDSHAPES.values() for name in names})


def _get_client():
    global _client
    if _client is None:
        _client = udp_client.SimpleUDPClient(config.VNYAN_OSC_HOST, config.VNYAN_OSC_PORT)
    return _client


def send_expression(expression_tag):
    """Set blendshape yang sesuai ekspresi jadi 1.0, semua yang lain 0.0, lewat
    protokol VMC standar — VNyan langsung apply ke model VRM tanpa node-graph
    tambahan. Dipanggil tiap Luno mau ganti mood avatar."""
    try:
        client = _get_client()
        active_names = set(_EXPRESSION_TO_BLENDSHAPES.get(expression_tag, _EXPRESSION_TO_BLENDSHAPES["neutral"]))

        for name in _ALL_BLENDSHAPE_NAMES:
            value = 1.0 if name in active_names else 0.0
            client.send_message("/VMC/Ext/Blend/Val", [name, value])

        client.send_message("/VMC/Ext/Blend/Apply", [1])
    except Exception as ex:
        print(f"[VNyan] ✗ Gagal kirim ekspresi (VMC): {ex}\n")
