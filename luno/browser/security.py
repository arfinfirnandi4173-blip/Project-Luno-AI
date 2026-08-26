"""
security.py (luno.browser)
============================

Three independent guardrails, each usable on its own so callers only pay
for what they need:

  - `is_domain_allowed()`      - optional allowlist (`BROWSER_ALLOWED_
                                  DOMAINS`). Empty allowlist = every
                                  domain permitted (opt-in restriction,
                                  same "empty = feature inactive"
                                  convention this project uses elsewhere -
                                  see `environment_intent.py`), NEVER a
                                  silent bypass once the list IS non-
                                  empty.
  - `redact_secrets()`         - strips anything that looks like a
                                  credential out of a string before it
                                  is ever logged or handed to an LLM.
  - `validate_download_path()` - blocks path traversal and refuses to
                                  silently overwrite a file outside the
                                  configured download directory.
  - `validate_download_directory()` - Sprint 66 (Tool Boundary
                                  Hardening) addition: the CONFIGURATION-
                                  level guard Sprint 65's own audit
                                  (Finding SPRINT65-002) found missing.
                                  `validate_download_path()` above only
                                  ever asked "does this destination stay
                                  inside `download_dir`?" - it had (and
                                  still has, as the inner containment
                                  check) no opinion on whether
                                  `download_dir` ITSELF is somewhere
                                  safe. This function is that missing
                                  outer check, applied both at
                                  `RealBrowserHandler` construction time
                                  (fail-closed startup validation - see
                                  `real_browser.py`) and again,
                                  defense-in-depth, immediately before
                                  every actual download
                                  (`real_browser.py::_dispatch()`'s
                                  `"download"` branch) - config can be
                                  reloaded without a restart in this
                                  module's own documented convention
                                  (see `config.py`'s docstring), so the
                                  per-call check is not redundant, it is
                                  the layer that actually matters if
                                  configuration changes mid-run.

None of these raise for "normal" bad input - each returns a
`(bool, reason)`-shaped verdict (or a redacted string) so the caller
decides what to do (log, refuse, ask for confirmation), matching this
project's "return a structured verdict, don't throw for expected
failure paths" convention (see `ToolResult.fail()`).
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse


def extract_domain(url: str) -> str:
    """Best-effort hostname extraction - never raises. `"github.com"`,
    `"http://github.com/foo"`, and `"github.com/foo"` (no scheme) all
    resolve to `"github.com"`."""
    if not url:
        return ""
    candidate = url.strip()
    if "://" not in candidate:
        candidate = "//" + candidate  # urlparse needs a scheme-relative form to find netloc without one
    try:
        host = urlparse(candidate).hostname or ""
    except Exception:
        return ""
    return host.lower()


def is_domain_allowed(url: str, allowed_domains: Iterable[str]) -> Tuple[bool, str]:
    """`allowed_domains` empty -> `(True, "no allowlist configured")` -
    every domain is permitted (see module docstring). Otherwise the
    URL's host must exactly match, or be a subdomain of, one of the
    allowed entries ("home.example.com" is allowed when "example.com"
    is on the list; "example.com.evil.com" is NOT, since it doesn't END
    in ".example.com" or equal "example.com" - a naive substring check
    would let that attack through)."""
    allowed = [a.strip().lower() for a in allowed_domains if a and a.strip()]
    if not allowed:
        return True, "no allowlist configured"
    host = extract_domain(url)
    if not host:
        return False, f"couldn't parse a hostname out of {url!r}"
    for domain in allowed:
        if host == domain or host.endswith("." + domain):
            return True, f"matches allowed domain '{domain}'"
    return False, f"'{host}' is not in BROWSER_ALLOWED_DOMAINS ({', '.join(allowed)})"


# -- credential redaction -------------------------------------------------------

#: Query-string/URL-embedded secrets - "?api_key=...", "?token=...", a
#: literal "user:pass@host" URL, an Authorization-style bearer token
#: pasted inline, etc. Deliberately broad/case-insensitive - a false-
#: positive redaction (blanking something that wasn't actually secret)
#: is a strictly acceptable cost; a missed real credential is not.
_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|pwd|access[_-]?key)\s*[=:]\s*[^&\s\"']+"),
    re.compile(r"(?i)(bearer)\s+[A-Za-z0-9\-\._~\+/]+=*"),
    re.compile(r"://[^/\s:]+:[^/\s@]+@"),  # user:pass@host URL form
]
_REDACTED = "[REDACTED]"


def redact_secrets(text: Optional[str]) -> str:
    """Never returns `None` (empty string for empty/`None` input) - safe
    to call unconditionally on anything about to be logged, put in a
    `ToolResult.message`, or handed to an LLM prompt. See module
    docstring: this is a best-effort net, not a cryptographic
    guarantee - real secrets should never reach this far in the first
    place (see `permissions.py`'s "the LLM only ever sees 'authentication
    required', never the actual secret" rule)."""
    if not text:
        return ""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda m: f"{m.group(1)}={_REDACTED}" if m.lastindex else _REDACTED, result)
    return result


# -- download path validation ---------------------------------------------------

def _path_contains(potential_ancestor: str, potential_descendant: str) -> bool:
    """`True` iff `potential_descendant` is `potential_ancestor` itself or
    lies inside it. Both inputs must already be resolved/normalized
    (realpath'd and normcase'd) by the caller - this function does no
    further resolution, it is purely the path-aware containment check
    (never a `str.startswith()` prefix comparison, which is unsafe: e.g.
    `"/a/b-evil"` naively "starts with" `"/a/b"` without being inside it -
    `os.path.commonpath()` compares path COMPONENTS, not raw characters).
    Handles the "different Windows drive" case (`os.path.commonpath`
    raises `ValueError` when given paths with no common drive/root) by
    treating that as "definitely not contained" rather than propagating
    the exception."""
    import os
    try:
        return os.path.commonpath([potential_ancestor, potential_descendant]) == potential_ancestor
    except ValueError:
        return False


def _resolve_for_comparison(path: str) -> str:
    """Canonical form used for every containment/equality check in this
    module: `os.path.realpath()` (resolves `.`/`..`, and - since Python
    3.8, on both POSIX and Windows - symlinks, junctions, and other
    reparse points; Windows resolution uses `GetFinalPathNameByHandleW`
    internally) followed by `os.path.normcase()` (a no-op on POSIX;
    lowercases and normalizes `/`->`\\` on Windows, where the filesystem
    is normally case-insensitive - without this, `C:\\Foo` and `c:\\foo`
    would compare as different paths and a case-varied escape would slip
    through). Works unchanged for paths that don't yet exist (realpath
    normalizes lexically without erroring on a missing target - needed
    since a not-yet-created download directory must still validate)."""
    import os
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def validate_download_path(destination: str, download_dir: str) -> Tuple[bool, str]:
    """A download's final on-disk path must resolve to somewhere INSIDE
    `download_dir` - blocks `../../etc/passwd`-style traversal AND an
    absolute path pointed somewhere else entirely. Returns
    `(True, resolved_path)` on success, `(False, reason)` on rejection -
    never raises, never actually touches the filesystem (that's the
    caller's job once this says yes).

    Sprint 66: upgraded from `os.path.abspath()` to `_resolve_for_
    comparison()` (realpath + normcase) for both `base` and `resolved` -
    the original `abspath`-only form normalized `.`/`..` but did NOT
    resolve symlinks, so a symlink placed inside (or in place of)
    `download_dir` pointing somewhere else could have bypassed this
    check even though `validate_download_directory()` (below) correctly
    resolves symlinks for the directory itself. This closes that gap
    without changing the function's contract for ordinary, non-symlinked
    input."""
    import os

    if not destination or not destination.strip():
        return False, "no destination filename given"
    if not download_dir or not download_dir.strip():
        return False, "no BROWSER_DOWNLOAD_DIR configured"

    base = _resolve_for_comparison(download_dir)
    # A bare filename is joined onto the ORIGINAL (not yet symlink-
    # resolved) `download_dir` before resolution, so a relative
    # destination is anchored the same way regardless of whether
    # `download_dir` itself turns out to be a symlink.
    candidate = destination if os.path.isabs(destination) else os.path.join(download_dir, destination)
    resolved = _resolve_for_comparison(candidate)

    if not _path_contains(base, resolved):
        return False, f"'{destination}' resolves outside the configured download directory ({resolved})"
    return True, resolved


# -- download DIRECTORY validation (Sprint 66) -----------------------------------

#: `luno/browser/security.py` lives at `<PROJECT_ROOT>/luno/browser/
#: security.py` - two `dirname()` calls up reaches the `luno/` package
#: directory itself (SOURCE_ROOT: every `.py` module this project ships,
#: nothing else), three reaches the repository root (PROJECT_ROOT:
#: everything - `luno/`, `config/`, `docs/`, `tests/`, `main.py`, `.env`).
#: Computed once, at import time, from `__file__` - never from any
#: environment variable, config value, or runtime input, so neither
#: constant can itself be influenced by configuration or conversation
#: text.
def _compute_roots() -> Tuple[str, str]:
    import os
    this_file = os.path.abspath(__file__)
    browser_dir = os.path.dirname(this_file)          # .../luno/browser
    source_root = os.path.dirname(browser_dir)          # .../luno
    project_root = os.path.dirname(source_root)          # ...
    return source_root, project_root


SOURCE_ROOT, PROJECT_ROOT = _compute_roots()


def _collect_critical_paths() -> Tuple[str, ...]:
    """Every individual file this project's own persistence/config
    layer is known to read or write, collected DYNAMICALLY from
    `luno.config`'s own module-level constants (every `NAME_FILE`
    attribute whose value is a string) rather than a hand-duplicated
    list that could silently drift out of sync with `luno/config.py`
    itself - this is the same enumeration technique Sprint 63/64/65's
    own tests already used to inventory these paths. Plus a small,
    explicit set of root-level files with no `config.py` constant of
    their own (the launchers, the license-guard doc, the dependency
    manifest, and `.env` - the credentials file). Returned as absolute
    paths (not yet resolved/normcased - `validate_download_directory()`
    does that itself, consistently, for every comparison)."""
    import os
    import luno.config as luno_config

    paths = []
    for name in vars(luno_config):
        if not name.endswith("_FILE"):
            continue
        value = getattr(luno_config, name)
        if not isinstance(value, str) or not value:
            continue
        paths.append(value if os.path.isabs(value) else os.path.join(PROJECT_ROOT, value))

    for fixed_name in (
        "main.py", "main_runtime_demo.py", "probe_memory_pipeline.py",
        "ARCHITECTURE_GUARD.md", "requirements.txt", ".env",
    ):
        paths.append(os.path.join(PROJECT_ROOT, fixed_name))

    return tuple(paths)


def validate_download_directory(download_dir: str) -> Tuple[bool, str]:
    """The OUTER guard `validate_download_path()` was always missing
    (Sprint 65's own Finding SPRINT65-002): is `download_dir` ITSELF
    somewhere safe, independent of whatever filename a caller later
    asks for? Returns `(True, resolved_path)` on success, `(False,
    reason)` on rejection - never raises, never touches the filesystem.

    The exact invariant enforced (see `docs/change_impact/
    tool_boundary_hardening.md` for the full design rationale,
    including why it is deliberately NOT "download_dir must be
    completely disjoint from PROJECT_ROOT" - that would break the
    current, correct, working default of `config/browser_downloads`,
    which legitimately nests under the project root):

      1. `download_dir` must not equal PROJECT_ROOT.
      2. `download_dir` must not equal SOURCE_ROOT (the `luno/` package).
      3. `download_dir` must not CONTAIN SOURCE_ROOT (can't be an
         ancestor of `luno/`).
      4. `download_dir` must not be CONTAINED BY SOURCE_ROOT (can't be
         nested inside `luno/`).
      5. `download_dir` must not be an ancestor of PROJECT_ROOT (can't
         be the whole filesystem, a parent directory, etc. - anything
         that would make PROJECT_ROOT itself a descendant of it).
      6. `download_dir` must not equal, and must not CONTAIN, any
         individual critical file from `_collect_critical_paths()`
         (every `config/*.json` persistence/config path, `.env`, the
         root-level launcher scripts, `ARCHITECTURE_GUARD.md`,
         `requirements.txt`).

    `download_dir` MAY legitimately be nested somewhere inside
    PROJECT_ROOT (e.g. `config/browser_downloads`) - that is the
    current, intended, working configuration and is NOT itself
    forbidden; only overlap with SOURCE_ROOT or an individual critical
    file is."""
    import os

    if not download_dir or not download_dir.strip():
        return False, "no download directory configured"

    try:
        resolved = _resolve_for_comparison(download_dir)
        source_root = _resolve_for_comparison(SOURCE_ROOT)
        project_root = _resolve_for_comparison(PROJECT_ROOT)
    except (ValueError, OSError) as ex:
        # malformed path (e.g. embedded NUL byte) - fail closed, never
        # leak the raw exception text (could echo back unexpected
        # control characters) beyond a short, generic reason.
        return False, f"could not resolve download directory: {type(ex).__name__}"

    if resolved == project_root:
        return False, "download directory must not be the project root itself"
    if resolved == source_root:
        return False, "download directory must not be the luno/ source package directory"
    if _path_contains(resolved, source_root):
        return False, "download directory must not contain the luno/ source package directory"
    if _path_contains(source_root, resolved):
        return False, "download directory must not be inside the luno/ source package directory"
    if _path_contains(resolved, project_root):
        return False, "download directory must not be an ancestor of the project root"

    for critical_path in _collect_critical_paths():
        try:
            critical_resolved = _resolve_for_comparison(critical_path)
        except (ValueError, OSError):
            continue
        if resolved == critical_resolved:
            return False, f"download directory must not equal a critical project file ({os.path.basename(critical_path)})"
        if _path_contains(resolved, critical_resolved):
            return False, f"download directory must not contain a critical project file ({os.path.basename(critical_path)})"

    return True, resolved
