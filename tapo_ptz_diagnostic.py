"""
tapo_ptz_diagnostic.py
========================

Sprint 70 (Tapo C212 Live Authentication & Auto-Recovery) - a small,
STRICTLY READ-ONLY diagnostic for the Tapo PAN/TILT (PTZ) connection,
run directly on the machine where `TAPO_HOST`/`TAPO_USERNAME`/
`TAPO_PASSWORD` are actually configured:

    python tapo_ptz_diagnostic.py

This is the Phase 1 "live connection diagnosis" step that could NOT be
performed from the sandbox that built this sprint (no `TAPO_HOST`/
`TAPO_USERNAME`/`TAPO_PASSWORD` configured there at all, and no network
route to a private-LAN camera regardless - see `docs/change_impact/
tapo_c212_live_recovery.md`'s own honest "LIVE VERIFICATION: NOT
POSSIBLE" section). Running this script IS that missing step - its
output is the single most valuable next data point for this sprint.

WHAT IT DOES: attempts exactly ONE real `pytapo.Tapo(host, user,
password)` construction (this performs the library's own real,
synchronous authentication - see `luno/tool_manager/builtin/
real_camera_ptz.py`'s module docstring for why), then classifies the
outcome using the EXACT SAME `classify_tapo_exception()` function the
production PTZ tool itself uses - so this script's output describes
precisely what the real tool would have seen, nothing invented, nothing
approximated.

WHAT IT NEVER DOES: never prints `TAPO_PASSWORD`, `TAPO_USERNAME`, or
any session/auth token - failure text is passed through this project's
own `_redact_credentials()` first, which strips the exact configured
credential VALUES before anything reaches stdout. Never issues a
`moveMotor`/`calibrateMotor`/`savePreset`/`setPreset` command - it does
NOT physically move your camera. Never writes to `config/*.json` or any
other file - purely a stdout report.

Exit code is always 0 (this is a diagnostic, not a pass/fail check) -
read the printed CATEGORY, not the exit code.
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import luno.config as legacy_config  # noqa: E402
from luno.tool_manager.builtin.real_camera_ptz import (  # noqa: E402
    classify_tapo_exception,
    _redact_credentials,
)


def main() -> int:
    print("Luno Tapo PTZ Diagnostic (Sprint 70) - READ-ONLY, never moves the camera, "
          "never prints credentials, never writes any file.\n")

    host_set = bool(legacy_config.TAPO_HOST)
    user_set = bool(legacy_config.TAPO_USERNAME)
    pass_set = bool(legacy_config.TAPO_PASSWORD)
    print(f"TAPO_HOST configured: {host_set}")
    print(f"TAPO_USERNAME configured: {user_set}")
    print(f"TAPO_PASSWORD configured: {pass_set}")
    # Deliberately never prints TAPO_HOST's actual value either - a
    # camera's LAN IP is low-sensitivity compared to credentials, but
    # this script has no need to print it to do its job, so it doesn't.

    if not (host_set and user_set and pass_set):
        print("\nRESULT: NOT_CONFIGURED")
        print("One or more of TAPO_HOST/TAPO_USERNAME/TAPO_PASSWORD is not set via this "
              "project's own configuration mechanism (.env / environment) - nothing to "
              "test. This is the exact same gap that made Sprint 69/70's own live "
              "verification impossible from the development sandbox.")
        return 0

    try:
        from pytapo import Tapo
    except Exception as ex:
        print(f"\nRESULT: PYTAPO_NOT_INSTALLED ({_redact_credentials(str(ex))})")
        print("The `pytapo` package isn't importable in this Python environment - "
              "install it with `pip install pytapo` and re-run.")
        return 0

    print("\nAttempting one real, live Tapo client construction (this performs "
          "pytapo's own real, synchronous authentication)...")
    try:
        Tapo(legacy_config.TAPO_HOST, legacy_config.TAPO_USERNAME, legacy_config.TAPO_PASSWORD)
    except Exception as ex:
        classified = classify_tapo_exception(ex)
        print(f"\nRESULT: {classified.category}")
        print(f"error_type: {classified.error_type}")
        print(f"retryable (per this sprint's own policy): {classified.retryable}")
        print(f"detail (credential-redacted): {_redact_credentials(str(ex))}")
        return 0

    print("\nRESULT: CONNECTED")
    print("The real Tapo client authenticated successfully. If Luno itself is still "
          "reporting a problem, the next place to look is whether "
          "CAMERA_PTZ_BACKEND=real is actually set (it's a separate switch from having "
          "credentials configured - see luno/bootstrap/launcher_config.py), or the "
          "SEPARATE luno.vision/dashboard camera-streaming path (a different subsystem "
          "entirely - see docs/change_impact/tapo_c212_authentication.md's own Phase 2 "
          "writeup for why these two are not the same connection).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
