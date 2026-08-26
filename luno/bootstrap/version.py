"""
version.py
==========

Single source of truth for the version/build string the startup banner
(and `/status`) print. No package in `luno/` needed a version number
before Sprint 6 - this is new, additive metadata only, nothing here is
read by any of the 19 existing subsystems.

`BUILD` is deliberately derived from the running interpreter's start
time-independent source: the file's own last-modified timestamp is NOT
used (this file barely changes), so `BUILD` is instead a short git-like
identifier when the project is a git checkout, falling back to a fixed
placeholder otherwise - either way, cheap, deterministic-enough for a
human glancing at the banner, and never a hard dependency (git is
optional; nothing breaks if it isn't installed or this isn't a repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

#: Sprint 6 introduces this - bump manually per release the same way any
#: other hand-maintained version string is bumped. Not read from
#: pyproject.toml/setup.py because neither exists in this project (see
#: Sprint 6 architecture report) and adding one is out of this sprint's
#: scope (build tooling, not runtime wiring).
VERSION = "1.0.0"

CODENAME = "Luno Runtime"


def _git_short_sha(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=2.0,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            return sha or None
    except Exception:
        pass
    return None


def build_string() -> str:
    """Best-effort build identifier: `<git-short-sha>` when this is a git
    checkout with git available, else `"local"`. Never raises - a
    launcher's version banner must never fail startup over this."""
    root = Path(__file__).resolve().parents[2]
    sha = _git_short_sha(root)
    return sha or "local"
