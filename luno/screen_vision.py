"""
screen_vision.py
==================

"Luno, screenshot terus liat kenapa error ini" - on-demand DESKTOP
screenshot + vision-provider diagnosis, READ-ONLY (Luno describes/
diagnoses what's on screen and suggests a fix; it never clicks/types
anything itself - see `luno/screen_intent.py`'s own docstring for why
this scope was chosen over full desktop computer-use).

Deliberately mirrors `luno/vision.py`'s `ask_vision()` shape (same
`{'description': str}` / `{'error': str}` return contract, same "never
raises" discipline) but captures the DESKTOP screen via Pillow's
`ImageGrab` instead of a camera frame via OpenCV - two independent
capture paths feeding the SAME vision provider singleton
(`luno.vision._get_vision_provider()` - reused on purpose, not
duplicated, so both features share one HTTP client/config and a test
can swap both via `luno.vision.set_vision_provider_for_testing()`).

Independent on/off switch (`SCREEN_VISION_ENABLED`) from
`CAMERA_VISION_ENABLED` - a user may want one without the other (no
webcam but wants screen diagnosis, or vice versa).

CATATAN JUJUR (honest limitations, same culture as
`vrm_idle_engine`'s own docstrings):
  - `ImageGrab.grab()` only captures the PRIMARY monitor's bounding box
    by default (Pillow's own documented behavior on Windows/macOS) - a
    problem visible on a second monitor won't be seen. Not solved here
    (`all_screens=True` support would change the image size
    unpredictably and hasn't been visually verified against a real
    multi-monitor setup).
  - Windows/macOS only - Pillow's `ImageGrab` module isn't available on
    Linux at all. `capture_screen()` degrades to an honest `None` (and
    `ask_screen()` to `{'error': ...}`) rather than crashing if the
    import/call fails, same discipline `luno.vision._capture_frame()`
    already applies to camera failures.
"""

from __future__ import annotations

import io
import threading
from typing import Any, Callable, Optional

from . import config

_JPEG_QUALITY = 70

_capture_lock = threading.Lock()


def is_configured() -> bool:
    """Master switch, same role as `luno.vision.is_configured()` -
    callers (the intent classifier, the handler in
    `main_runtime_demo.py`) check this FIRST so a user who never opted
    in never has a screenshot silently taken."""
    return bool(config.SCREEN_VISION_ENABLED)


def capture_screen(grab_fn: Optional[Callable[[], Any]] = None):
    """Grabs one screenshot of the primary monitor as a Pillow `Image`,
    or `None` on failure (Pillow's `ImageGrab` unavailable on this OS,
    no display/permission, etc.) - never raises.

    `grab_fn` is an injectable override purely for tests (this project's
    CI/sandbox environment has no real display to screenshot) - same
    dependency-injection convention `RealFishAudioClient`'s
    `synthesize_fn` param and `RequestsOpenRouterClient`'s `session`
    param already established elsewhere in this project. Production
    code never passes it, defaulting to the real `ImageGrab.grab`.

    Locked (same reasoning as `vision._capture_frame()`'s `_camera_lock`)
    purely so two near-simultaneous screen requests don't interleave -
    `ImageGrab.grab()` itself is cheap/stateless per call, this is
    belt-and-suspenders, not a real contention point."""
    with _capture_lock:
        try:
            if grab_fn is not None:
                return grab_fn()
            from PIL import ImageGrab  # lazy import - Windows/macOS only, see module docstring
            return ImageGrab.grab()
        except Exception as ex:
            print(f"[ScreenVision] ⚠ Gagal ambil screenshot: {ex}")
            return None


def _encode_screenshot_for_upload(image) -> Optional[bytes]:
    """Downscale (long edge -> `config.SCREEN_VISION_MAX_EDGE`, same
    reasoning as `luno.vision._encode_frame_for_upload`'s own resize) +
    JPEG-encode a Pillow `Image` for upload. Returns `None` if `image`
    is falsy or encoding fails - never raises."""
    if image is None:
        return None
    to_encode = image
    try:
        max_edge = config.SCREEN_VISION_MAX_EDGE
        width, height = image.size
        longest = max(width, height)
        if max_edge and longest > max_edge:
            scale = max_edge / float(longest)
            new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
            to_encode = image.resize(new_size)
    except Exception as ex:
        print(f"[ScreenVision] ⚠ Resize gagal sebelum upload (lanjut pakai ukuran asli): {ex}")
        to_encode = image
    try:
        buf = io.BytesIO()
        to_encode.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
        return buf.getvalue()
    except Exception as ex:
        print(f"[ScreenVision] ⚠ Gagal encode screenshot: {ex}")
        return None


_DEFAULT_QUESTION = (
    "Lihat screenshot layar ini. Jelasin apa yang keliatan bermasalah/error "
    "(kalau ada) secara singkat dan natural, dan saranin cara benerinnya. "
    "Kalau nggak ada yang keliatan salah, bilang aja layarnya keliatan normal."
)


def ask_screen(question: str = "", grab_fn: Optional[Callable[[], Any]] = None):
    """Ambil screenshot layar SEKARANG, lalu tanyain ke vision provider
    yang SAMA dipakai `luno.vision.ask_vision()` (Gemini/OpenAI, pilih
    lewat `VISION_PROVIDER` - lihat modul docstring). Return dict
    `{'description': str}` kalau sukses, `{'error': str}` kalau gagal
    (fitur belum diaktifin, screenshot gagal, provider gagal, dst) -
    SEMUA pesan Bahasa Indonesia yang jelas, never raises - same
    contract as `ask_vision()`, so callers (see
    `main_runtime_demo.py::_handle_screen_intent`) can treat both
    identically."""
    if not is_configured():
        return {
            "error": (
                "Fitur screenshot belum diaktifin. Set SCREEN_VISION_ENABLED=true "
                "di .env kalau mau Luno bisa lihat layar."
            )
        }

    question = (question or "").strip() or _DEFAULT_QUESTION

    image = capture_screen(grab_fn)
    if image is None:
        return {
            "error": (
                "Nggak bisa ambil screenshot. Cek apakah OS-nya didukung "
                "(Windows/macOS) dan Luno punya izin akses layar."
            )
        }

    image_bytes = _encode_screenshot_for_upload(image)
    if image_bytes is None:
        return {"error": "Gagal encode screenshot buat diupload."}

    import luno.vision as vision_module  # reuse the SAME provider singleton/config on purpose - see module docstring
    provider = vision_module._get_vision_provider()
    try:
        description = provider.analyze_image(image_bytes, question)
    except vision_module.VisionProviderError as ex:
        return {"error": f"Vision gagal baca screenshot: {ex}"}
    except Exception as ex:
        # Never let an unexpected exception from a third-party dependency
        # escape this function - same discipline as
        # vision._query_vision_provider()'s own bare except.
        return {"error": f"Vision error nggak terduga: {ex}"}

    if not description or not description.strip():
        return {"error": "Vision provider ngasih balasan kosong."}

    print(f"[ScreenVision] ✓ '{question[:60]}' → {description[:80]}...")
    return {"description": description}
