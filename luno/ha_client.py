"""
Klien WebSocket ke Home Assistant.

Modul ini murni "jalur komunikasi" ke HA: connect, auth, subscribe event, call
service, ambil snapshot state. Dia TIDAK tahu apa-apa soal lampu/switch/script
spesifik (itu urusan devices.py) — supaya bisa dites/dipakai ulang sendiri.
"""

import json
import asyncio
import websockets

from .config import HA_WS_URL, HA_TOKEN


class HomeAssistantClient:
    def __init__(self):
        self.ws = None
        self.msg_id = 1
        self.connected = False
        self.pending_responses = {}
        self.call_lock = asyncio.Lock()

    async def connect(self):
        try:
            print(f"\n[HA] Connecting to {HA_WS_URL}...")
            self.pending_responses.clear()  # buang sisa state dari koneksi sebelumnya (kalau reconnect)
            self.ws = await asyncio.wait_for(websockets.connect(HA_WS_URL), timeout=10)
            print("[HA] ✓ WebSocket connected")

            auth_msg = await asyncio.wait_for(self.ws.recv(), timeout=5)
            auth_data = json.loads(auth_msg)

            if auth_data.get("type") == "auth_required":
                await self.ws.send(json.dumps({
                    "type": "auth",
                    "access_token": HA_TOKEN
                }))

                auth_result = await asyncio.wait_for(self.ws.recv(), timeout=5)
                result = json.loads(auth_result)

                if result.get("type") == "auth_ok":
                    print("[HA] ✓ Authenticated\n")
                    self.connected = True
                    return True
        except Exception as ex:
            print(f"[HA] ✗ Connection failed: {ex}\n")
            return False

    async def subscribe_to_events(self):
        try:
            await self.ws.send(json.dumps({
                "id": self.msg_id,
                "type": "subscribe_events",
                "event_type": "state_changed"
            }))
            self.msg_id += 1
            print("[HA] ✓ Subscribed to events")
        except Exception:
            pass

    async def listen_and_dispatch(self, callback):
        try:
            while self.connected:
                try:
                    message = await asyncio.wait_for(self.ws.recv(), timeout=30)
                    data = json.loads(message)

                    if data.get("type") == "result" and data.get("id"):
                        msg_id = data.get("id")
                        if msg_id in self.pending_responses:
                            self.pending_responses[msg_id] = data

                    elif data.get("type") == "event":
                        event_data = data.get("event", {})
                        if event_data.get("event_type") == "state_changed":
                            await callback(event_data.get("data", {}))

                except asyncio.TimeoutError:
                    try:
                        await self.ws.ping()
                    except Exception:
                        pass

        except Exception as ex:
            print(f"[HA] Listen error: {ex}")
            self.connected = False

    async def get_states(self):
        """Ambil snapshot semua state entity saat ini dari HA (dipakai buat state-query
        supaya Luno tahu kondisi device tanpa harus menunggu event state_changed)."""
        async with self.call_lock:
            try:
                if not self.connected:
                    return []

                msg_id = self.msg_id
                self.msg_id += 1

                self.pending_responses[msg_id] = None
                await self.ws.send(json.dumps({"id": msg_id, "type": "get_states"}))

                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if msg_id in self.pending_responses and self.pending_responses[msg_id]:
                        result = self.pending_responses.pop(msg_id)
                        if result.get("success"):
                            return result.get("result", [])
                        return []

                self.pending_responses.pop(msg_id, None)
                print("[HA] ✗ get_states timeout\n")
                return []
            except Exception as ex:
                print(f"[HA] ✗ get_states error: {ex}\n")
                return []

    async def call_service(self, domain, service, entity_id, data=None):
        async with self.call_lock:
            try:
                if not self.connected:
                    print("[HA] ✗ Not connected")
                    return False

                if data is None:
                    data = {}

                data["entity_id"] = entity_id

                msg_id = self.msg_id
                self.msg_id += 1

                service_call = {
                    "id": msg_id,
                    "type": "call_service",
                    "domain": domain,
                    "service": service,
                    "service_data": data
                }

                print(f"[HA] → {domain}.{service}")

                self.pending_responses[msg_id] = None
                await self.ws.send(json.dumps(service_call))

                # Wait for response
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    if msg_id in self.pending_responses and self.pending_responses[msg_id]:
                        result = self.pending_responses[msg_id]
                        del self.pending_responses[msg_id]

                        if result.get("success"):
                            print("[HA] ✓ Done\n")
                            return True
                        else:
                            error_info = result.get("error", {})
                            err_code = error_info.get("code", "unknown")
                            err_msg = error_info.get("message", "no message")
                            print(f"[HA] ✗ Failed — {err_code}: {err_msg}\n")
                            return False

                if msg_id in self.pending_responses:
                    del self.pending_responses[msg_id]
                print("[HA] ✗ Timeout\n")
                return False

            except Exception as ex:
                print(f"[HA] ✗ Error: {ex}\n")
                return False

    async def disconnect(self):
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.connected = False