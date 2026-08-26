"""
Bridge ke `vrm_idle_engine` — animation engine full-body procedural (breathing,
weight shift, gaze, blink, finger idle, random gesture, dst, lihat
`vrm_idle_engine/README.md`) yang beda dari `vnyan_idle.py` (idle motion versi
lama, lebih simpel: cuma head sway + beberapa gesture pose tangan hard-coded).

Backend baru: `AVATAR_BACKEND=vnyan_engine` (beda dari `AVATAR_BACKEND=vnyan`
yang lama, yang masih pakai `vnyan_bridge.py` + `vnyan_idle.py`) — jadi ganti
sekali di .env, TIDAK menggantikan/menghapus backend "vnyan" yang lama, dua-
duanya tetap ada dan bisa dipilih salah satu.

Kenapa 2 backend VNyan (bukan langsung ganti yang lama): `vnyan_idle.py` udah
"kepake"/di-tuning sebelumnya, jadi biar aman kalau engine baru ternyata perlu
tuning lebih lanjut di sisi kamu (lihat CATATAN JUJUR di bawah), tinggal balik
`AVATAR_BACKEND=vnyan` tanpa kehilangan apa pun.

DEFAULT: `body_focus="chest_up"` — animasi tangan/lengan/kaki DIMATIKAN (permintaan
terakhir), avatar cuma gerak dari dada ke atas (napas, bahu, leher, kepala, mata,
kedip, ekspresi wajah). Lengan tetap "digantung" natural (bukan T-pose), cuma
diem — bukan dihapus dari skeleton. Ganti balik ke gerak full body kapan aja
lewat `set_body_focus("full_body")` di bawah kalau nanti mau nyalain lagi.

Thread model: SAMA kayak `vnyan_idle.py` — `start()` jalanin loop 60 FPS
`AnimationController.run()` di thread daemon terpisah, `stop()` matiin lewat
`AnimationController.stop()` (loop keluar abis frame yang lagi jalan kelar,
bukan langsung dibunuh paksa).

CATATAN JUJUR: `vrm_idle_engine` sendiri (liat README-nya) udah bilang jujur
soal ini — beberapa angka (arah lengan/rest pose) itu TEBAKAN yang belum
diverifikasi visual langsung di VNyan kamu (nggak ada cara render VRM dari
lingkungan yang bikin ini). Karena `body_focus="chest_up"` MATIIN animasi
lengan sama sekali, itu nggak relevan buat sekarang — begitu nanti mau nyalain
`full_body` lagi, jalanin `python vrm_idle_engine/tools/arm_tuner.py --port
<VNYAN_OSC_PORT>` buat cek/tuning arah lengan dulu sebelum dipakai serius.
"""

import threading

from . import config

_controller = None
_thread = None
_running = False
_lock = threading.Lock()

# Tag ekspresi internal Luno (dari expressions.guess_expression(), SAMA
# persis dengan yang dipakai vnyan_bridge.py) -> AvatarState engine baru.
# Engine-nya sendiri punya state lebih banyak (embarrassed/excited/sleepy)
# yang belum ada tag pemicunya di Luno — tetap kepake kalau nanti
# expressions.py ditambah pola baru buat itu, cukup tambah baris di sini.
_EXPRESSION_TO_STATE_NAME = {
    "happy": "happy",
    "laughing": "excited",   # lebih "hidup" dari sekadar happy
    "sad": "sad",
    "angry": "angry",
    "surprised": "excited",  # nggak ada AvatarState.SURPRISED, excited paling deket
    "thinking": "thinking",
    "neutral": "idle",
}


def _get_controller():
    """Bikin `AnimationController` sekali aja (lazy import — biar modul ini
    nggak nge-crash kalau vrm_idle_engine belum di-copy/hilang, SELAMA
    AVATAR_BACKEND bukan 'vnyan_engine')."""
    global _controller
    if _controller is None:
        from vrm_idle_engine.config.settings import EngineConfig
        from vrm_idle_engine.controller.animation_controller import AnimationController

        engine_config = EngineConfig()
        engine_config.vmc.host = config.VNYAN_OSC_HOST
        engine_config.vmc.port = config.VNYAN_OSC_PORT
        engine_config.body_focus = "chest_up"
        _controller = AnimationController(engine_config)
    return _controller


def set_body_focus(mode):
    """'full_body' atau 'chest_up' — bisa dipanggil kapan aja SETELAH start(),
    nggak perlu restart. Cuma flip .enabled per-layer (murah, aman dipanggil
    tiap saat, termasuk pas loop lagi jalan)."""
    controller = _get_controller()
    is_chest_up = mode == "chest_up"
    for name in ("weight_shift", "arms", "legs", "fingers"):
        layer = controller.animator.get_layer(name)
        if layer is not None:
            layer.enabled = not is_chest_up
    controller.config.body_focus = mode
    print(f"[VNyan Engine] body_focus -> {mode}\n")


def start():
    """Jalanin AnimationController.run() (loop 60 FPS) di thread daemon
    terpisah. Idempotent — aman dipanggil berkali-kali."""
    global _running, _thread
    with _lock:
        if _running:
            return
        controller = _get_controller()
        _running = True
        _thread = threading.Thread(target=controller.run, daemon=True)
        _thread.start()
        print(
            f"[VNyan Engine] ✓ Full-body procedural idle loop jalan "
            f"({controller.config.fps} FPS, body_focus={controller.config.body_focus}) "
            f"-> {controller.config.vmc.host}:{controller.config.vmc.port}\n"
        )


def stop():
    global _running
    with _lock:
        if not _running or _controller is None:
            return
        _running = False
        _controller.stop()


def set_thinking(is_thinking):
    """Dipanggil dari avatar_dispatch.send_thinking()."""
    from vrm_idle_engine.controller.state_machine import AvatarState

    controller = _get_controller()
    controller.set_state(AvatarState.THINKING if is_thinking else AvatarState.IDLE)


def set_speaking(is_speaking):
    """Dipanggil dari avatar_dispatch.send_speaking(). CUMA toggle mulut
    (TalkingLayer, lewat controller.set_talking) — SENGAJA nggak ganti
    AvatarState ke/dari TALKING, soalnya AvatarState.TALKING nggak punya
    entri ekspresi/postur sendiri (lihat emotion.py) DAN cross-fade state
    bakal nge-fade OUT ekspresi yang barusan di-set lewat send_expression()
    (mis. 'happy') kalau dipaksa pindah state di sini. Biarin state ekspresi
    yang lagi aktif TETAP kepake, cuma mulutnya yang gerak lipsync-nya."""
    controller = _get_controller()
    controller.set_talking(bool(is_speaking))


def send_expression(expression_tag):
    """Dipanggil dari avatar_dispatch.send_speech()/send_expression() —
    cross-fade AvatarState sesuai tag ekspresi (lihat
    _EXPRESSION_TO_STATE_NAME di atas buat mapping-nya)."""
    from vrm_idle_engine.controller.state_machine import AvatarState

    controller = _get_controller()
    state_name = _EXPRESSION_TO_STATE_NAME.get(expression_tag, "idle")
    controller.set_state(AvatarState(state_name))
