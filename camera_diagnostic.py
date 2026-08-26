"""
camera_diagnostic.py
=====================

Sprint 69 (Camera Device / OpenCV Stability Fix) - a small, STRICTLY
READ-ONLY diagnostic you can run directly on the device to see what
`luno.vision` actually finds:

    python camera_diagnostic.py
    python camera_diagnostic.py --max-index 8 --timeout 3.0

Prints, per candidate device index: whether it's available/unavailable/
busy/backend-error, which backend actually worked (if any), how long the
open took, whether a frame could be read, resolution, and FPS if
reported. Also prints the CURRENTLY CONFIGURED `camera_source()` (what
`CAMERA_INDEX`/`CAMERA_URL` actually resolve to right now) and its own
`camera_status()`/last-known state, so you can see whether the app's own
configured source lines up with what discovery found.

READ-ONLY: this script never writes to `config.CAMERA_INDEX`/
`config.CAMERA_URL`, never touches any `config/*.json` file, and never
changes which camera source the real running application uses - it only
calls `luno.vision.discover_cameras()` (see that function's own
docstring), which opens/closes candidate devices transiently but leaves
nothing modified afterward. Nothing here modifies production config
automatically, per the Sprint 69 brief's own explicit instruction.

Sprint 69.1: the configured source is now printed through
`vision._classify_source_for_log()` (e.g. "network(scheme=rtsp,
host=192.168.1.55)"), never as the raw `camera_source()` value - a
`CAMERA_URL` can legitimately embed camera credentials
(`rtsp://user:pass@host/...`), and printing it verbatim (the pre-69.1
behavior of this script) would have leaked them to stdout/terminal
scrollback/redirected log files. This is the same fix applied to
`luno/vision.py`'s own diagnostic logging - see `docs/change_impact/
camera_runtime_dashboard_forensics.md`.
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import luno.vision as vision  # noqa: E402


def _fmt_state(state: str) -> str:
    icons = {
        "AVAILABLE": "OK",
        "UNAVAILABLE": "--",
        "BUSY": "!!",
        "BACKEND_ERROR": "XX",
        "UNKNOWN": "??",
    }
    return f"[{icons.get(state, '??')}] {state}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only camera discovery/diagnostic (Sprint 69). "
                    "Never modifies production config."
    )
    parser.add_argument("--max-index", type=int, default=vision.DISCOVER_CAMERAS_DEFAULT_MAX_INDEX,
                         help=f"how many device indices (0..N-1) to probe "
                              f"(default: {vision.DISCOVER_CAMERAS_DEFAULT_MAX_INDEX})")
    parser.add_argument("--timeout", type=float, default=None,
                         help="per-backend-candidate open timeout in seconds "
                              "(default: config.CAMERA_OPEN_TIMEOUT_S)")
    args = parser.parse_args(argv)

    print("Luno Camera Diagnostic (Sprint 69) - READ-ONLY, does not change any config\n")

    source = vision.camera_source()
    status = vision.camera_status()
    print(f"Configured camera_source(): {vision._classify_source_for_log(source)}")
    print(f"Last known camera_status(): connected={status['connected']} "
          f"state={status['state']} reason={status['state_reason']!r} "
          f"cooldown_remaining_s={status['cooldown_remaining_s']}\n")

    # Sprint 69.1: print the actual backend candidates this platform/
    # installed OpenCV build will request for a LOCAL source, so it's
    # immediately checkable from this one run whether the Sprint 69 fix
    # is actually active for this machine (e.g. an empty/`[None]` list
    # here on Windows would mean neither CAP_DSHOW nor CAP_MSMF resolved
    # to a real cv2 attribute - a genuine "the fix fell back to CAP_ANY"
    # signal worth investigating).
    local_candidates = [vision._backend_label(b) for b in vision._local_backend_candidates()]
    print(f"Platform: {platform.system()}  |  local-source backend candidates: {local_candidates}\n")

    print(f"Probing device indices 0..{args.max_index - 1} "
          f"(timeout={args.timeout if args.timeout is not None else 'config default'}s/candidate)...\n")

    results = vision.discover_cameras(max_index=args.max_index, timeout_s=args.timeout)

    header = f"{'idx':>3}  {'state':<22}  {'backend':<14}  {'open_ms':>8}  {'read_ok':<7}  {'read_ms':>8}  {'resolution':<12}  {'fps':>6}  reason"
    print(header)
    print("-" * len(header))
    for entry in results:
        print(
            f"{entry['index']:>3}  "
            f"{_fmt_state(entry['state']):<22}  "
            f"{str(entry['backend_used'] or '-'): <14}  "
            f"{('' if entry['open_time_ms'] is None else entry['open_time_ms']):>8}  "
            f"{str(entry['read_ok']):<7}  "
            f"{('' if entry['read_time_ms'] is None else entry['read_time_ms']):>8}  "
            f"{str(entry['resolution'] or '-'): <12}  "
            f"{('' if entry['fps'] is None else entry['fps']):>6}  "
            f"{entry['reason'] or ''}"
        )

    available = [e for e in results if e["state"] == "AVAILABLE"]
    print(f"\n{len(available)} of {len(results)} probed indices reported AVAILABLE.")
    if not available:
        print(
            "No camera responded on any probed index. This could mean: no camera is "
            "physically connected, the OS is blocking camera access (check Windows "
            "camera privacy settings), the camera is claimed by another application, "
            "or the correct index is outside the probed range (try --max-index higher)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
