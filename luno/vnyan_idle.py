"""
Idle motion parametrik buat VNyan lewat VMC "/VMC/Ext/Bone/Pos" — port LANGSUNG dari
logic idle animation di avatar.html (Three.js) ke Python, biar hasilnya KONSISTEN
sama yang udah kita tuning bareng di web (nengok pelan/"lirik-lirik", weight-shift
pinggul, tilt kepala sesekali, nafas halus) — cuma sekarang targetnya avatar VNyan,
bukan Three.js.

TAMBAHAN: sistem GESTURE POSE (silang tangan, lihat tangan) — beda dari sway halus
di atas, ini pose PENUH (banyak bone lengan/tangan sekaligus) yang dipegang beberapa
detik lalu balik normal, biar avatar nggak cuma "napas + nengok" doang tapi kadang
ada gerakan yang lebih "berarti". Lihat IDLE_GESTURES di bawah buat nambah/ubah pose.

BEDA PENTING dari vnyan_bridge.send_expression() (yang dikirim SEKALI tiap ganti
mood): loop di sini JALAN TERUS-MENERUS di background (~30 FPS) selama Luno hidup,
ngirim rotasi bone kepala/spine/dada/pinggul/lengan yang berubah pelan-pelan tiap frame.

⚠ PENTING kalau VNyan kamu JUGA punya idle motion bawaan yang gerakin bone yang SAMA
(kepala/spine/hips/lengan) — bisa "rebutan"/jitter/gontok-gontokan sama data yang
kita kirim di sini. Kalau itu kejadian, matiin idle motion bawaan VNyan KHUSUS buat
bone-bone itu (biarin Python yang kontrol), fitur lain (kedip, lipsync, physics
rambut) biarin tetep VNyan yang urus sendiri.

CATATAN JUJUR: saya nggak punya VNyan buat test langsung — format pesan VMC-nya
udah sesuai spesifikasi resmi, tapi nama bone humanoid ("Head"/"Spine"/"Chest"/
"Hips"/"LeftUpperArm"/dst) itu konvensi umum Unity/VRM, KEMUNGKINAN perlu
disesuaikan kalau ternyata VNyan versi kamu pakai casing/nama beda. Dan ANGKA
ROTASI di IDLE_GESTURES itu TEBAKAN AWAL — cross_arms/look_down_at_hands hampir
pasti perlu di-tuning manual sambil liat hasilnya langsung (saya nggak bisa
verifikasi visual dari sini). Kalau bone-nya nggak gerak sama sekali, cek nama
bone dulu; kalau gerak tapi posenya aneh/nembus badan, itu waktunya tuning angka.
"""

import math
import random
import threading
import time

from pythonosc import udp_client

from . import config

_client = None
_running = False
_thread = None

# State idle — SAMA PERSIS konsepnya kayak avatar.html, biar hasilnya konsisten
# antara backend web dan VNyan.
_look_target = {"x": 0.0, "y": 0.0}
_look_current = {"x": 0.0, "y": 0.0}
_next_look_change_at = 0.0

_tilt_target = 0.0
_tilt_current = 0.0
_next_tilt_change_at = 0.0

_hip_shift_target = 0.0
_hip_shift_current = 0.0
_next_hip_shift_at = 0.0

_is_thinking = False

# ─────────────────────────────────────────────
#  GESTURE POSE (silang tangan, lihat tangan, dll) — beda dari sway halus di atas:
#  ini pose PENUH (banyak bone sekaligus), dipegang beberapa detik, baru ganti lagi.
# ─────────────────────────────────────────────
#
# Tiap gesture: dict {nama_bone: (rot_x, rot_y, rot_z)} dalam radian, RELATIF ke rest
# pose (bukan T-pose mentah). "normal" = kosong = semua bone di sini balik ke 0.
#
# ⚠ ANGKA-ANGKA DI SINI TEBAKAN AWAL, BUKAN HASIL TUNING VISUAL — saya nggak punya
# cara render VRM buat ngecek langsung. Kemungkinan BESAR perlu kamu sesuaikan
# sambil liat hasilnya di VNyan (naik-turunin nilainya dikit-dikit sampai pas).
IDLE_GESTURES = {
    "normal": {},

    "look_down_at_hands": {
        "Head": (0.35, 0.0, 0.0),
        "Neck": (0.15, 0.0, 0.0),
        "LeftUpperArm": (0.3, 0.2, 0.9),
        "LeftLowerArm": (0.0, 0.6, 0.0),
        "RightUpperArm": (0.3, -0.2, -0.9),
        "RightLowerArm": (0.0, -0.6, 0.0),
    },

    "cross_arms": {
        "Head": (0.05, 0.0, 0.0),
        "LeftUpperArm": (1.0, 0.4, 1.0),
        "LeftLowerArm": (0.0, 1.3, 0.0),
        "LeftHand": (0.0, 0.0, 0.3),
        "RightUpperArm": (1.0, -0.4, -1.0),
        "RightLowerArm": (0.0, -1.3, 0.0),
        "RightHand": (0.0, 0.0, -0.3),
    },
}

# Pose "lagi ngejelasin" — dipakai KHUSUS pas Luno lagi ngomong (lihat set_speaking()
# di bawah), beda dari IDLE_GESTURES di atas (yang buat pas DIEM/nggak ngomong).
# Tangan lebih "aktif"/terbuka, kesan lagi jelasin sesuatu — bukan pose pasif.
SPEAKING_GESTURES = {
    "explain_open_hands": {
        "LeftUpperArm": (0.2, 0.1, 0.5),
        "LeftLowerArm": (0.0, 0.3, 0.0),
        "RightUpperArm": (0.2, -0.1, -0.5),
        "RightLowerArm": (0.0, -0.3, 0.0),
    },
    "explain_one_hand": {
        "RightUpperArm": (0.3, -0.2, -0.6),
        "RightLowerArm": (0.0, -0.5, 0.0),
        "RightHand": (0.0, 0.0, -0.2),
    },
    "explain_small_gesture": {
        "LeftUpperArm": (0.1, 0.05, 0.35),
        "LeftLowerArm": (0.0, 0.15, 0.0),
    },
}

# Semua bone yang PERNAH dipakai gesture (idle ATAU speaking) manapun di atas —
# dipakai buat tau bone mana aja yang perlu di-blend balik ke rest pose.
_GESTURE_BONES = sorted({
    bone
    for gesture_set in (IDLE_GESTURES, SPEAKING_GESTURES)
    for g in gesture_set.values()
    for bone in g
})

# Rest pose BASELINE buat bone lengan — ini yang jadi acuan "normal" (BUKAN (0,0,0),
# karena (0,0,0) itu literally T-pose di ruang rotasi normalized VRM). Tanpa ini,
# tiap kali gesture balik ke "normal", lengan bakal nge-lerp balik ke T-pose dulu
# sebelum ke posisi natural — kelihatan "kedip" T-pose sekilas pas transisi.
# Sama persis konsepnya kayak applyRestPose() di avatar.html (Three.js).
_REST_POSE = {
    "LeftUpperArm": (0.0, 0.0, 1.15),
    "RightUpperArm": (0.0, 0.0, -1.15),
    "LeftLowerArm": (0.0, 0.0, 0.15),
    "RightLowerArm": (0.0, 0.0, -0.15),
}

_current_gesture = "normal"
_next_gesture_change_at = 0.0
# Current rotation per bone (di-lerp tiap frame) — mulai dari rest pose (BUKAN nol),
# biar nggak ada kedipan T-pose sekilas pas Luno baru nyala/loop baru mulai.
_gesture_blend = {bone: list(_REST_POSE.get(bone, (0.0, 0.0, 0.0))) for bone in _GESTURE_BONES}

_is_speaking = False


def _maybe_pick_new_gesture(now):
    global _current_gesture, _next_gesture_change_at
    if _is_speaking:
        return  # lagi ngomong -> set_speaking() yang pegang kendali gesture, bukan cycling normal
    if now < _next_gesture_change_at:
        return
    if _current_gesture != "normal":
        # Abis pegang pose beberapa detik, SELALU balik normal dulu sebelum
        # (mungkin) pilih gesture lain — biar nggak loncat pose ke pose lain
        # tanpa transisi lewat posisi netral.
        _current_gesture = "normal"
        _next_gesture_change_at = now + 8 + random.random() * 14  # diem normal 8-22 detik
    else:
        choices = [g for g in IDLE_GESTURES if g != "normal"]
        _current_gesture = random.choice(choices)
        _next_gesture_change_at = now + 6 + random.random() * 6  # pegang pose 6-12 detik
        print(f"[VNyan] 🎭 Gesture: {_current_gesture}\n")


def set_speaking(is_speaking):
    """Dipanggil dari avatar_dispatch.send_speaking() — SELAMA Luno ngomong, gesture
    idle biasa (cross_arms/look_down_at_hands) DIBEKUKAN, diganti pose 'lagi
    ngejelasin' dari SPEAKING_GESTURES (dipilih random tiap Luno mulai ngomong).
    Begitu selesai ngomong, balik ke siklus idle normal. Full VMC, nggak ada
    dependency ke Trigger API/Node Graph sama sekali."""
    global _is_speaking, _current_gesture, _next_gesture_change_at
    _is_speaking = bool(is_speaking)

    if _is_speaking:
        _current_gesture = random.choice(list(SPEAKING_GESTURES.keys()))
        print(f"[VNyan] 🗣️ Speaking gesture: {_current_gesture}\n")
    else:
        _current_gesture = "normal"
        _next_gesture_change_at = time.time() + 3 + random.random() * 5  # jeda dikit sebelum idle gesture lain mulai lagi


def _get_client():
    global _client
    if _client is None:
        _client = udp_client.SimpleUDPClient(config.VNYAN_OSC_HOST, config.VNYAN_OSC_PORT)
    return _client


def _euler_to_quaternion(x, y, z):
    """Konversi rotasi Euler (radian, urutan XYZ) jadi quaternion (qx, qy, qz, qw).
    Buat rotasi sekecil ini (idle sway), urutan axis nggak terlalu berasa bedanya
    secara visual — jadi nggak perlu presisi 100% match sama convention VNyan."""
    cx, sx = math.cos(x / 2), math.sin(x / 2)
    cy, sy = math.cos(y / 2), math.sin(y / 2)
    cz, sz = math.cos(z / 2), math.sin(z / 2)

    qw = cx * cy * cz + sx * sy * sz
    qx = sx * cy * cz - cx * sy * sz
    qy = cx * sy * cz + sx * cy * sz
    qz = cx * cy * sz - sx * sy * cz
    return qx, qy, qz, qw


def _send_bone(client, bone_name, rot_x, rot_y, rot_z):
    qx, qy, qz, qw = _euler_to_quaternion(rot_x, rot_y, rot_z)
    # Posisi selalu (0,0,0) — kita CUMA ubah rotasi, bukan geser posisi bone,
    # biar proporsi tubuh nggak keliatan stretch/aneh.
    client.send_message("/VMC/Ext/Bone/Pos", [bone_name, 0.0, 0.0, 0.0, qx, qy, qz, qw])


def set_thinking(is_thinking):
    """Dipanggil dari avatar_dispatch.send_thinking() — modulasi idle motion pas
    Luno lagi mikir (kepala dikit nunduk + goyang pelan, sama kayak versi web)."""
    global _is_thinking
    _is_thinking = bool(is_thinking)


def _maybe_pick_new_look(now):
    global _look_target, _next_look_change_at
    if now < _next_look_change_at:
        return
    _look_target = {
        "x": (random.random() - 0.5) * 0.25,
        "y": (random.random() - 0.5) * 0.12,
    }
    _next_look_change_at = now + 2.5 + random.random() * 3.5


def _maybe_pick_new_tilt(now):
    global _tilt_target, _next_tilt_change_at
    if now < _next_tilt_change_at:
        return
    _tilt_target = 0.0 if random.random() < 0.6 else (1 if random.random() < 0.5 else -1) * (0.08 + random.random() * 0.09)
    _next_tilt_change_at = now + 3.0 + random.random() * 4.5


def _maybe_pick_new_hip_shift(now):
    global _hip_shift_target, _next_hip_shift_at
    if now < _next_hip_shift_at:
        return
    _hip_shift_target = (random.random() - 0.5) * 0.05
    _next_hip_shift_at = now + 4.0 + random.random() * 5.0


def _loop():
    global _look_current, _tilt_current, _hip_shift_current

    client = _get_client()
    start_time = time.time()
    last_time = start_time
    fps = 30
    frame_time = 1.0 / fps

    print(f"[VNyan] ✓ Idle motion loop jalan ({fps} FPS) -> {config.VNYAN_OSC_HOST}:{config.VNYAN_OSC_PORT}\n")

    while _running:
        frame_start = time.time()
        now = frame_start
        delta = now - last_time
        last_time = now
        t = now - start_time

        _maybe_pick_new_look(now)
        _maybe_pick_new_tilt(now)
        _maybe_pick_new_hip_shift(now)
        _maybe_pick_new_gesture(now)

        _look_current["x"] += (_look_target["x"] - _look_current["x"]) * delta * 1.2
        _look_current["y"] += (_look_target["y"] - _look_current["y"]) * delta * 1.2
        _tilt_current += (_tilt_target - _tilt_current) * delta * 1.5
        _hip_shift_current += (_hip_shift_target - _hip_shift_current) * delta * 0.8

        # Blend semua bone gesture (lengan/tangan/leher) ke target gesture aktif —
        # bone yang nggak disebut di gesture sekarang di-lerp balik ke REST POSE-nya
        # (arms-down natural), BUKAN (0,0,0) — soalnya (0,0,0) itu T-pose mentah.
        # _current_gesture bisa berasal dari IDLE_GESTURES (lagi diem) ATAU
        # SPEAKING_GESTURES (lagi ngomong) — cek dua-duanya.
        active_gesture = IDLE_GESTURES.get(_current_gesture) or SPEAKING_GESTURES.get(_current_gesture) or {}
        for bone in _GESTURE_BONES:
            default_target = _REST_POSE.get(bone, (0.0, 0.0, 0.0))
            target = active_gesture.get(bone, default_target)
            cur = _gesture_blend[bone]
            for i in range(3):
                cur[i] += (target[i] - cur[i]) * delta * 1.0  # blend ~1 detik ke pose baru

        thinking_tilt_x = 0.15 if _is_thinking else 0.0
        thinking_sway_y = math.sin(t * 0.8) * 0.08 if _is_thinking else 0.0

        # Head: kalau gesture lagi PEGANG pose yang nentuin Head sendiri, itu yang
        # menang (gesture override sway) — biar nggak numpuk 2 sumber rotasi kepala
        # yang saling tarik-tarikan. Kalau gesture nggak nentuin Head (termasuk pas
        # "normal"), balik pakai sway/look-around/thinking biasa.
        if "Head" in active_gesture:
            head_x, head_y, head_z = _gesture_blend["Head"]
        else:
            head_x = _look_current["y"] + thinking_tilt_x
            head_y = _look_current["x"] + thinking_sway_y
            head_z = _tilt_current

        spine_z = math.sin(t * 0.6) * 0.012 + math.sin(t * 0.23) * 0.006
        chest_x = math.sin(t * 1.2) * 0.004
        hips_z = _hip_shift_current

        try:
            _send_bone(client, "Head", head_x, head_y, head_z)
            _send_bone(client, "Spine", 0.0, 0.0, spine_z)
            _send_bone(client, "Chest", chest_x, 0.0, 0.0)
            _send_bone(client, "Hips", 0.0, 0.0, hips_z)
            for bone in _GESTURE_BONES:
                if bone == "Head":
                    continue  # udah dikirim di atas
                rx, ry, rz = _gesture_blend[bone]
                _send_bone(client, bone, rx, ry, rz)
        except Exception as ex:
            print(f"[VNyan] ✗ Gagal kirim idle bone pos: {ex}\n")

        elapsed = time.time() - frame_start
        time.sleep(max(0, frame_time - elapsed))


def start():
    """Jalanin idle motion loop di thread terpisah. Idempotent — aman dipanggil
    berkali-kali, nggak bakal start dobel kalau udah jalan."""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()


def stop():
    global _running
    _running = False
