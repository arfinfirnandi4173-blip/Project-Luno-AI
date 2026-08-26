"""
Loop reconnect ke Home Assistant. Ini "lem" yang menghubungkan ha_client.py
(jalur komunikasi) dengan devices.py (registry controller) — begitu koneksi
berhasil, dia bangun semua controller sekali, lalu terus mendengarkan event
sampai koneksi putus, lalu otomatis coba reconnect (dengan exponential backoff).

ha_loop di modul ini adalah event loop milik thread HA. SEMUA pemanggilan
coroutine ke ha_client dari thread lain (mis. dari Luno_Brain di thread utama)
harus lewat `asyncio.run_coroutine_threadsafe(coro, ha_listener.ha_loop)`.
"""

import asyncio

from . import devices

ha_loop = None  # diisi oleh start_ha_listener() begitu event loop-nya jalan


async def start_ha_listener(ha_client):
    global ha_loop

    ha_loop = asyncio.get_running_loop()
    backoff = 2  # detik, naik tiap gagal (exponential), reset tiap connect sukses

    while True:
        if await ha_client.connect():
            backoff = 2
            await ha_client.subscribe_to_events()

            # Controller cuma dibangun SEKALI (idempotent, lihat devices.build_devices).
            devices.build_devices(ha_client)

            # PENTING: listen_and_dispatch() harus SUDAH jalan (di background) sebelum
            # kita panggil get_states()/refresh_all_device_states() — karena yang benar-benar
            # membaca balasan dari WebSocket dan ngisi pending_responses itu listen_and_dispatch(),
            # bukan get_states() itu sendiri. Kalau belum ada yang "mendengarkan", get_states()
            # nunggu respons yang gak akan pernah datang -> selalu timeout.
            listen_task = asyncio.create_task(ha_client.listen_and_dispatch(devices.handle_ha_event))

            print("[HA] ✓ Ready, listening for events...\n")
            await devices.refresh_all_device_states(ha_client)

            await listen_task  # tetap di sini selama koneksi ini hidup
            print(f"[HA] ✗ Disconnected — mencoba reconnect dalam {backoff}s...\n")
        else:
            print(f"[HA] ✗ Gagal connect — mencoba lagi dalam {backoff}s...\n")

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)  # cap di 60 detik biar gak nunggu kelamaan


def run_ha_listener(ha_client):
    """Entry point buat dijalankan di thread terpisah, mis:
    threading.Thread(target=run_ha_listener, args=(ha_client,), daemon=True).start()
    """
    try:
        asyncio.run(start_ha_listener(ha_client))
    except Exception as ex:
        print(f"[HA] Error: {ex}")