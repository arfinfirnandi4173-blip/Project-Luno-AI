"""
Bridge WebSocket antara Luno (backend Python) dan avatar 3D (avatar.html, jalan di
browser pakai Three.js + @pixiv/three-vrm).

Alur koneksinya: avatar.html yang CONNECT KE Luno (bukan sebaliknya) — jadi kamu bisa
buka/refresh/tutup tab browser avatar-nya kapan aja tanpa perlu restart Luno. Luno
cuma broadcast event (audio + teks + tag ekspresi) ke semua client yang lagi connect;
kalau nggak ada yang connect, broadcast-nya no-op (aman, nggak error).

Python nggak bisa render grafis 3D sendiri — makanya avatar-nya proses terpisah
(browser), dan modul ini yang jadi jembatan komunikasinya.
"""

import asyncio
import json
import base64
import websockets

from . import config

_clients = set()
avatar_loop = None  # diisi begitu server-nya jalan, dipakai broadcast() dari thread lain


async def _handler(websocket):
    _clients.add(websocket)
    print(f"[Avatar] ✓ Client connected ({len(_clients)} total)\n")
    try:
        async for _ in websocket:
            pass  # satu arah aja (Luno -> avatar), nggak butuh nerima apa-apa dari browser
    except Exception:
        pass
    finally:
        _clients.discard(websocket)
        print(f"[Avatar] Client disconnected ({len(_clients)} total)\n")


async def _serve():
    global avatar_loop
    avatar_loop = asyncio.get_running_loop()
    async with websockets.serve(_handler, "localhost", config.AVATAR_WS_PORT):
        print(f"[Avatar] ✓ Bridge ready at ws://localhost:{config.AVATAR_WS_PORT} — buka avatar.html buat connect\n")
        await asyncio.Future()  # jalan selamanya


def run_avatar_bridge():
    """Entry point buat dijalankan di thread terpisah, mis:
    threading.Thread(target=run_avatar_bridge, daemon=True).start()"""
    try:
        asyncio.run(_serve())
    except Exception as ex:
        print(f"[Avatar] ✗ Error: {ex}\n")


async def _broadcast_async(event):
    if not _clients:
        return
    payload = json.dumps(event)
    dead = set()
    for ws in list(_clients):
        try:
            await ws.send(payload)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


def broadcast(event):
    """Kirim 1 event (dict) ke semua avatar client yang lagi connect. Aman dipanggil
    dari thread MANA PUN (termasuk main thread) — no-op kalau bridge belum jalan atau
    belum ada client yang connect, jadi nggak pernah bikin Luno error/nge-hang."""
    if not avatar_loop:
        return
    asyncio.run_coroutine_threadsafe(_broadcast_async(event), avatar_loop)


def send_speech(text, audio_bytes, expression="neutral"):
    """Broadcast 1 giliran bicara: teks + audio WAV (di-encode base64 biar bisa lewat
    JSON) + tag ekspresi buat avatar ganti pose muka. Dipanggil dari speak() di main.py,
    di titik yang sama tempat audio itu didapat dari GPT-SoVITS."""
    broadcast({
        "type": "speak",
        "text": text,
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "expression": expression,
    })


def send_expression(expression):
    """Ganti ekspresi avatar TANPA ngomong (mis. buat idle mood)."""
    broadcast({"type": "expression", "expression": expression})


def send_thinking(is_thinking):
    """Kasih tau avatar Luno lagi 'mikir' (nunggu balasan GPT) atau udah selesai —
    dipanggil dari process_and_respond() di main.py, membungkus SETIAP pemanggilan
    Luno_Brain() (baik yang cepat/deterministik maupun yang lambat lewat GPT)."""
    broadcast({"type": "thinking", "thinking": bool(is_thinking)})