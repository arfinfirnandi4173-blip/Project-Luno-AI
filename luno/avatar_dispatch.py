"""
Router tipis: pilih backend avatar berdasarkan config.AVATAR_BACKEND, supaya main.py
manggil SATU set fungsi yang sama (send_speech/send_thinking/send_expression) tanpa
perlu tau/peduli avatar-nya jalan di avatar.html (WebSocket), VNyan (OSC/VMC), atau
dimatikan total (AVATAR_BACKEND=none).

Ganti backend = ganti AVATAR_BACKEND di .env, TIDAK PERLU sentuh main.py sama sekali.
"""

from . import config
from . import avatar_bridge
from . import vnyan_bridge
from . import vnyan_idle
from . import vnyan_engine_bridge


def send_speech(text, audio_bytes, expression="neutral"):
    """Broadcast 1 giliran bicara. Di backend 'web', ini ngirim teks+audio+ekspresi
    lewat WebSocket. Di backend 'vnyan'/'vnyan_engine', audio-nya TIDAK dikirim
    lewat sini (VNyan dapet audio buat lipsync dari virtual audio cable, bukan
    dari kita) — cuma ekspresinya yang dikirim lewat OSC/VMC. Di 'none', no-op
    total."""
    if config.AVATAR_BACKEND == "none":
        return
    if config.AVATAR_BACKEND == "vnyan":
        vnyan_bridge.send_expression(expression)
    elif config.AVATAR_BACKEND == "vnyan_engine":
        vnyan_engine_bridge.send_expression(expression)
    else:
        avatar_bridge.send_speech(text, audio_bytes, expression)


def send_thinking(is_thinking):
    if config.AVATAR_BACKEND == "none":
        return
    if config.AVATAR_BACKEND == "vnyan":
        if config.VNYAN_IDLE_MOTION:
            vnyan_idle.set_thinking(is_thinking)
    elif config.AVATAR_BACKEND == "vnyan_engine":
        vnyan_engine_bridge.set_thinking(is_thinking)
    else:
        avatar_bridge.send_thinking(is_thinking)


def send_expression(expression):
    if config.AVATAR_BACKEND == "none":
        return
    if config.AVATAR_BACKEND == "vnyan":
        vnyan_bridge.send_expression(expression)
    elif config.AVATAR_BACKEND == "vnyan_engine":
        vnyan_engine_bridge.send_expression(expression)
    else:
        avatar_bridge.send_expression(expression)


def send_speaking(is_speaking):
    """Kasih tau avatar Luno lagi ngomong/selesai.
    - 'vnyan': trigger pose 'lagi ngejelasin' dari SPEAKING_GESTURES di
      vnyan_idle.py (cuma kalau VNYAN_IDLE_MOTION aktif).
    - 'vnyan_engine': toggle TalkingLayer (mulut) di vrm_idle_engine, TANPA
      ganti AvatarState (lihat vnyan_engine_bridge.set_speaking's docstring).
    - 'web': nggak butuh ini, avatar.html udah otomatis tau lewat event
      'speak' yang isinya audio itu sendiri.
    Di 'none', no-op total."""
    if config.AVATAR_BACKEND == "none":
        return
    if config.AVATAR_BACKEND == "vnyan" and config.VNYAN_IDLE_MOTION:
        vnyan_idle.set_speaking(is_speaking)
    elif config.AVATAR_BACKEND == "vnyan_engine":
        vnyan_engine_bridge.set_speaking(is_speaking)
