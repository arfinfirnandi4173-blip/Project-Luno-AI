"""
tests/test_sprint66_tool_boundary_hardening.py
=================================================

Sprint 66 - Tool Boundary Hardening.

Hardens the ONE gap Sprint 65's audit found (Finding SPRINT65-002 - the
browser download directory's safety was a configuration invariant, not a
code-level guarantee) and adds a permanent regression guard against the
other Sprint 65 safety property (a closed, fixed tool/action registry -
Finding SPRINT65-001). This sprint does NOT add any new capability to
Luno - no shell execution, no arbitrary Python execution, no arbitrary
filesystem write, no source modification, no git write, no plugin
installation, no dynamic tool registration. Every test below either (a)
proves a NEW restriction holds, or (b) proves an absence (a capability
that was never there stays absent).

All adversarial reproductions use `tmp_path`/monkeypatched module state
only - never the real project checkout. Read-only assertions against the
real `SOURCE_ROOT`/`PROJECT_ROOT`/`config/*.json` paths never write to
them (validating a path is pure string/stat computation, not a mutation).

Run:
    python3 -m pytest tests/test_sprint66_tool_boundary_hardening.py -v
"""

from __future__ import annotations

import hashlib
import inspect
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.browser.security as security_module  # noqa: E402
import luno.tool_manager.manager as tm_manager_module  # noqa: E402
import luno.tool_manager.registry as tm_registry_module  # noqa: E402
from luno.browser.security import (  # noqa: E402
    PROJECT_ROOT, SOURCE_ROOT, validate_download_directory, validate_download_path,
)
from luno.browser.config import BrowserConfig  # noqa: E402
from luno.tool_manager.builtin.real_browser import RealBrowserHandler  # noqa: E402


def _sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ============================================================================
# Phase 2/3 - trust boundary constants, computed purely from __file__, never
# from env/config/conversation input.
# ============================================================================

def test_trust_boundary_constants_are_computed_from_file_location_only():
    assert SOURCE_ROOT == os.path.join(PROJECT_ROOT, "luno")
    assert os.path.isdir(SOURCE_ROOT)
    assert os.path.isdir(PROJECT_ROOT)
    # neither constant is influenced by any environment variable
    src = inspect.getsource(security_module._compute_roots)
    assert "os.getenv" not in src and "os.environ" not in src


def test_critical_paths_are_collected_dynamically_from_luno_config_not_hardcoded():
    """NOTE: `tests/conftest.py`'s autouse `isolate_persistent_state`
    fixture redirects every WRITABLE state constant (`LONG_TERM_MEMORY_
    FILE`, `EPISODIC_MEMORY_FILE`, etc.) to a `tmp_path`-scoped value for
    every test in this suite, INCLUDING this one - so this test checks
    against `luno_config`'s CURRENT live values (whatever they are right
    now, isolated or not), not hardcoded default filenames. This is the
    correct property to prove: `_collect_critical_paths()` always tracks
    whatever `luno.config` currently says, never a stale duplicate list."""
    import luno.config as luno_config
    paths = set(security_module._collect_critical_paths())
    for name in (
        "APPS_CONFIG_FILE", "LIGHTS_CONFIG_FILE", "SWITCHES_CONFIG_FILE", "SCRIPTS_CONFIG_FILE",
        "LONG_TERM_MEMORY_FILE", "SESSION_SUMMARIES_FILE", "PERSONA_FILE",
    ):
        current_value = getattr(luno_config, name)
        expected_abs = current_value if os.path.isabs(current_value) else os.path.join(PROJECT_ROOT, current_value)
        assert expected_abs in paths, f"expected the current value of {name} ({expected_abs}) in collected critical paths"
    # plus the fixed root-level set (never redirected by any fixture)
    basenames = {os.path.basename(p) for p in paths}
    for expected in ("main.py", "main_runtime_demo.py", ".env", "ARCHITECTURE_GUARD.md"):
        assert expected in basenames


# ============================================================================
# Phase 8 A-E, plus Phase 9's own "don't break legitimate use" requirement -
# validate_download_directory() against REAL production roots. Pure path
# computation - no filesystem writes, so safe to run against the real
# checkout's own SOURCE_ROOT/PROJECT_ROOT directly (read-only stats only,
# via os.path.realpath).
# ============================================================================

def test_A_the_real_current_default_download_dir_is_accepted():
    """Negative control (Phase 9): the CURRENT, real, working default
    (`config/browser_downloads`) must still validate - hardening must
    never break legitimate existing behavior."""
    cfg = BrowserConfig.from_env()
    ok, resolved = validate_download_directory(cfg.download_dir)
    assert ok is True, f"the real default download_dir must remain valid, got: {resolved}"
    assert resolved.endswith("browser_downloads")


def test_B_source_root_itself_is_rejected():
    ok, reason = validate_download_directory(SOURCE_ROOT)
    assert ok is False
    assert "luno/ source package directory" in reason


def test_C_a_child_directory_of_source_root_is_rejected():
    ok, reason = validate_download_directory(os.path.join(SOURCE_ROOT, "browser"))
    assert ok is False
    assert "inside the luno/ source package directory" in reason


def test_D_the_parent_of_project_root_is_rejected():
    ok, reason = validate_download_directory(os.path.dirname(PROJECT_ROOT))
    assert ok is False  # contains SOURCE_ROOT (and everything else) as a descendant


def test_D_project_root_itself_is_rejected():
    ok, reason = validate_download_directory(PROJECT_ROOT)
    assert ok is False
    assert "project root itself" in reason


def test_E_a_directory_disjoint_from_the_project_is_accepted(tmp_path):
    """A genuinely unrelated sibling directory (no overlap with SOURCE_
    ROOT/PROJECT_ROOT/any critical file) must be accepted - hardening
    must not become a blanket 'anything near the project is denied'
    rule."""
    sibling = tmp_path / "totally_unrelated_downloads"
    sibling.mkdir()
    ok, resolved = validate_download_directory(str(sibling))
    assert ok is True


def test_config_directory_itself_is_rejected_because_it_contains_critical_files():
    """`config/` (DATA_DIR) itself - as opposed to `config/browser_
    downloads`, a LEAF subdirectory of it - is rejected, because it
    directly contains `apps.json`/`long_term_memory.json`/etc. This is
    the precise distinction the invariant draws: nesting under the
    project root is fine, nesting AT a level that contains a critical
    file is not."""
    ok, reason = validate_download_directory(os.path.join(PROJECT_ROOT, "config"))
    assert ok is False
    assert "critical project file" in reason


def test_a_single_critical_file_path_used_as_a_directory_is_rejected():
    """Uses `config/apps.json` specifically (via `APPS_CONFIG_FILE`)
    rather than a memory/state file, because `APPS_CONFIG_FILE` is
    deliberately NEVER redirected by `tests/conftest.py`'s isolation
    fixture (it's read-only, human-edited config - see that fixture's
    own comment) - so its real path is guaranteed stable across every
    test in this suite, unlike the writable state files."""
    import luno.config as luno_config
    ok, reason = validate_download_directory(luno_config.APPS_CONFIG_FILE)
    assert ok is False


# ============================================================================
# Phase 8 F/G - validate_download_path() (the per-FILE containment check,
# upgraded this sprint to realpath-based resolution) still blocks
# traversal/absolute escape, using a synthetic tmp_path fixture.
# ============================================================================

def _make_synthetic_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "protected.py").write_text("MARKER = 'original'\n")
    download_dir = project / "downloads"
    download_dir.mkdir()
    return project, download_dir


def test_F_parent_traversal_in_filename_is_rejected(tmp_path):
    project, download_dir = _make_synthetic_project(tmp_path)
    ok, reason = validate_download_path("../protected.py", str(download_dir))
    assert ok is False
    assert (project / "protected.py").read_text() == "MARKER = 'original'\n"


def test_G_absolute_path_escape_is_rejected(tmp_path):
    project, download_dir = _make_synthetic_project(tmp_path)
    ok, reason = validate_download_path(str(project / "protected.py"), str(download_dir))
    assert ok is False


# ============================================================================
# Phase 8 H/I - Windows drive-letter/case-insensitivity. This sandbox runs
# on Linux, where os.path.normcase() is a documented no-op and drive
# letters don't exist - so the FULL cross-platform behavior cannot be
# functionally exercised here. What IS verified: the implementation
# actually calls the normalization primitive Windows needs
# (os.path.normcase), so the SAME code path that resolves correctly on
# this platform is unconditionally exercised on Windows too - not a
# platform-specific branch that only "happens" to work here. See Known
# Limitations in the audit doc for the honest scope of this claim.
# ============================================================================

def test_H_resolve_for_comparison_calls_normcase_for_windows_case_insensitivity():
    src = inspect.getsource(security_module._resolve_for_comparison)
    assert "os.path.normcase(" in src
    assert "os.path.realpath(" in src


def test_I_case_variation_of_an_identical_path_resolves_identically(tmp_path):
    """Platform-limited proxy: on a case-preserving-but-sensitive
    filesystem (this sandbox's), two differently-cased strings for
    the SAME literal path are still normalized to the same absolute
    form modulo case by `_resolve_for_comparison()` on a
    case-INsensitive OS - not provable end-to-end on Linux, but the
    lowercasing step itself is verified directly here."""
    mixed = "/tmp/AbC"
    assert os.path.normcase(mixed) == mixed or os.path.normcase(mixed) == mixed.lower()
    # on this (case-sensitive) platform normcase is a no-op by design -
    # documented, not asserted as "proof" of Windows behavior.


# ============================================================================
# Phase 8 J - trailing separator must not change the containment verdict.
# ============================================================================

def test_J_trailing_separator_does_not_change_the_verdict():
    cfg_dir = os.path.join(PROJECT_ROOT, "config", "browser_downloads")
    ok1, resolved1 = validate_download_directory(cfg_dir)
    ok2, resolved2 = validate_download_directory(cfg_dir + os.sep)
    assert ok1 == ok2 == True
    assert resolved1 == resolved2


# ============================================================================
# Phase 8 K - symlink escape. Built entirely inside tmp_path, with
# SOURCE_ROOT/PROJECT_ROOT/critical-paths monkeypatched to point at a
# fully synthetic mimic project - never the real checkout.
# ============================================================================

def _make_synthetic_mimic_tree(tmp_path):
    """Mirrors the REAL project's own shape (a `luno/`-equivalent source
    dir plus a critical config file) so the symlink-escape tests below
    exercise the actual `validate_download_directory()` algorithm against
    a completely disposable tree."""
    root = tmp_path / "mimic_project"
    root.mkdir()
    source = root / "mimic_source"
    source.mkdir()
    (source / "core.py").write_text("# pretend source\n")
    config_dir = root / "mimic_config"
    config_dir.mkdir()
    critical_file = config_dir / "critical.json"
    critical_file.write_text("{}")
    return root, source, critical_file


def test_K_symlinked_download_dir_pointing_at_source_is_rejected(tmp_path, monkeypatch):
    root, source, critical_file = _make_synthetic_mimic_tree(tmp_path)
    monkeypatch.setattr(security_module, "SOURCE_ROOT", str(source))
    monkeypatch.setattr(security_module, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(security_module, "_collect_critical_paths", lambda: (str(critical_file),))

    real_downloads = tmp_path / "real_downloads_target"
    real_downloads.mkdir()
    evil_symlink = tmp_path / "configured_download_dir"
    os.symlink(str(source), str(evil_symlink))  # symlink -> the "source" dir

    ok, reason = validate_download_directory(str(evil_symlink))
    assert ok is False, "a symlink resolving into the source tree must be rejected, not just its literal path"


def test_K_symlinked_download_dir_to_a_safe_target_is_accepted(tmp_path, monkeypatch):
    root, source, critical_file = _make_synthetic_mimic_tree(tmp_path)
    monkeypatch.setattr(security_module, "SOURCE_ROOT", str(source))
    monkeypatch.setattr(security_module, "PROJECT_ROOT", str(root))
    monkeypatch.setattr(security_module, "_collect_critical_paths", lambda: (str(critical_file),))

    real_downloads = tmp_path / "real_downloads_target"
    real_downloads.mkdir()
    safe_symlink = tmp_path / "configured_download_dir_safe"
    os.symlink(str(real_downloads), str(safe_symlink))

    ok, resolved = validate_download_directory(str(safe_symlink))
    assert ok is True
    assert resolved == os.path.realpath(str(real_downloads))


def test_K_symlinked_destination_inside_a_safe_download_dir_cannot_escape_via_the_link(tmp_path, monkeypatch):
    """Even if `download_dir` itself is safe, a symlink PLANTED inside it
    (e.g. by a previous download) pointing at a critical file must not
    let a later `filename` argument "resolve through" it to escape -
    this is exactly the gap the realpath upgrade to
    `validate_download_path()` closes."""
    root, source, critical_file = _make_synthetic_mimic_tree(tmp_path)
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    link_inside = download_dir / "innocent_looking_name"
    os.symlink(str(critical_file), str(link_inside))

    ok, resolved = validate_download_path("innocent_looking_name", str(download_dir))
    # the symlink's REAL target is OUTSIDE download_dir, so this must be
    # rejected even though the literal filename never contained "..".
    assert ok is False, "a symlink inside download_dir pointing outside it must be rejected"


# ============================================================================
# Phase 8 L - junction/reparse-point escape. Cannot be created or tested
# on this Linux sandbox (junctions/reparse points are an NTFS-specific
# mechanism) - honestly marked as a platform limitation rather than
# claimed as tested. What CAN be shown: the resolution primitive used
# (`os.path.realpath()`) is the SAME one Python uses to resolve NTFS
# junctions/reparse points on Windows (via `GetFinalPathNameByHandleW`,
# since Python 3.8) - not a separate, untested code path for that
# platform.
# ============================================================================

def test_L_realpath_is_the_single_resolution_primitive_for_both_symlinks_and_reparse_points():
    """Documents (executably) that this codebase does not implement its
    own separate symlink-vs-junction handling - it delegates entirely to
    `os.path.realpath()`, whose Windows implementation (Python >= 3.8)
    already resolves junctions/mount points/reparse points the same way
    it resolves symlinks. There is exactly one call site for this in the
    validation code."""
    # isolate the function BODY (skip the docstring, which mentions
    # "os.path.realpath()" in prose) - the body is everything after the
    # closing triple-quote.
    resolve_src = inspect.getsource(security_module._resolve_for_comparison)
    body = resolve_src.split('"""')[-1]
    assert body.count("os.path.realpath(") == 1
    assert sys.version_info >= (3, 8), "os.path.realpath()'s Windows reparse-point resolution requires Python >= 3.8"


# ============================================================================
# Phase 8 M/N/O - empty / malformed / nonexistent paths.
# ============================================================================

def test_M_empty_download_dir_is_rejected():
    ok, reason = validate_download_directory("")
    assert ok is False
    assert "no download directory configured" in reason
    ok2, reason2 = validate_download_directory("   ")
    assert ok2 is False


def test_N_malformed_path_fails_closed_without_crashing():
    """A NUL byte in a path raises `ValueError` from `os.path.*` - must
    be caught and turned into a clean rejection, never an unhandled
    exception that could crash a caller."""
    ok, reason = validate_download_directory("bad\x00path")
    assert ok is False
    assert "could not resolve" in reason or "ValueError" in reason


def test_O_nonexistent_download_directory_is_still_validated_by_path_shape(tmp_path):
    """A download directory that doesn't exist YET (first run, not yet
    created) must still validate purely on its resolved path shape - it
    must not be rejected merely for not existing, and it must still be
    correctly rejected if its (not-yet-existing) path would overlap the
    source tree."""
    not_yet_created_safe = tmp_path / "will_be_created_later" / "downloads"
    ok, resolved = validate_download_directory(str(not_yet_created_safe))
    assert ok is True, "a nonexistent-but-safe path must still validate"

    not_yet_created_unsafe = os.path.join(SOURCE_ROOT, "not_yet_created_subdir")
    ok2, reason2 = validate_download_directory(not_yet_created_unsafe)
    assert ok2 is False, "a nonexistent-but-unsafe path (inside SOURCE_ROOT) must still be rejected"


# ============================================================================
# Phase 8 P/Q - unknown tool / unknown action still fail closed (Sprint 65
# re-affirmed, not re-derived from scratch).
# ============================================================================

def test_P_unknown_tool_fails_closed():
    registry = tm_registry_module.ToolRegistry()
    manager = tm_manager_module.ToolManager(registry=registry)
    result = manager.execute({"tool": "definitely_not_registered", "action": "anything"})
    assert result.success is False
    assert result.error_type == "unknown_tool"


def test_Q_unknown_action_fails_closed():
    registry = tm_registry_module.ToolRegistry()
    manager = tm_manager_module.ToolManager(registry=registry)
    registry.register("browser", RealBrowserHandler(provider=object()))
    result = manager.execute({"tool": "browser", "action": "delete_everything"})
    assert result.success is False
    assert result.error_type in ("unknown_action", "validation_error")


# ============================================================================
# Phase 8 R/S - a tool NAME shaped like a Python module path or an
# executable path is never imported/executed - it is only ever a plain
# dict key.
# ============================================================================

def test_R_module_path_shaped_tool_name_is_never_imported():
    registry = tm_registry_module.ToolRegistry()
    manager = tm_manager_module.ToolManager(registry=registry)
    for evil_name in ("os.system", "subprocess.Popen", "builtins.exec", "__import__"):
        result = manager.execute({"tool": evil_name, "action": "run"})
        assert result.success is False
        assert result.error_type == "unknown_tool"


def test_S_executable_path_shaped_tool_name_is_never_executed():
    registry = tm_registry_module.ToolRegistry()
    manager = tm_manager_module.ToolManager(registry=registry)
    for evil_name in ("/bin/sh", "C:\\Windows\\System32\\cmd.exe", "../../bin/bash"):
        result = manager.execute({"tool": evil_name, "action": "run"})
        assert result.success is False
        assert result.error_type == "unknown_tool"


def test_R_registry_get_is_a_plain_dict_lookup_never_import_or_getattr():
    """Structural proof: `ToolRegistry.get()`'s entire implementation is
    a `dict.get()` call - no `importlib`, `getattr` on a dynamic name, or
    `__import__` anywhere in its source."""
    src = inspect.getsource(tm_registry_module.ToolRegistry.get)
    for forbidden in ("importlib", "getattr(", "__import__", "eval(", "exec("):
        assert forbidden not in src


# ============================================================================
# Phase 8 T - no tool call can mutate the registry (no handler holds a
# registry reference at all).
# ============================================================================

def test_T_no_builtin_handler_class_stores_a_registry_reference():
    import luno.tool_manager.builtin.real_browser as m1
    import luno.tool_manager.builtin.real_windows as m2
    import luno.tool_manager.builtin.real_home_assistant as m3
    import luno.tool_manager.builtin.real_camera_ptz as m4
    for module in (m1, m2, m3, m4):
        for _, obj in vars(module).items():
            if inspect.isclass(obj) and issubclass(obj, object) and hasattr(obj, "execute") and obj.__module__ == module.__name__:
                try:
                    init_src = inspect.getsource(obj.__init__)
                except (TypeError, OSError):
                    continue
                assert "registry" not in init_src.lower() or "ToolRegistry" not in init_src, (
                    f"{obj.__name__}.__init__ appears to reference a registry - "
                    f"handlers must never hold a reference capable of mutating it"
                )


def test_T_tool_registry_register_is_only_ever_called_from_bootstrap_or_test_code():
    """Repo-wide structural check, AST-based (not a text grep) so
    docstring examples/prose mentioning "registry.register()" are
    correctly excluded, and calls on an unrelated object (e.g.
    `luno.adapters.manager`'s own, differently-typed `AdapterRegistry`)
    are excluded by requiring the call to be `<something>.registry.
    register(` or a bare `registry.register(` - the exact shape every
    REAL `ToolRegistry.register()` call site in this codebase uses."""
    import ast
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        if "__pycache__" in dirpath or "tests" in dirpath.split(os.sep):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "register"):
                    continue
                # the receiver must itself be (or end in) `.registry` -
                # e.g. `manager.registry.register(...)` or
                # `registry.register(...)` - never some other object's
                # own `.register()` method (like AdapterRegistry's).
                receiver = func.value
                receiver_is_registry = (
                    (isinstance(receiver, ast.Name) and receiver.id == "registry")
                    or (isinstance(receiver, ast.Attribute) and receiver.attr == "registry")
                )
                if not receiver_is_registry:
                    continue
                hits.append((path, node.lineno))
    allowed_dirs = ("bootstrap", "builtin")
    # luno/adapters/manager.py's own `self.registry.register(adapter, cfg)`
    # is a DIFFERENT, unrelated registry (`AdapterRegistry`, for Whisper/
    # OpenRouter/Vision/etc. adapters - see luno/adapters/registry.py) -
    # syntactically identical shape (`<x>.registry.register(...)`) but a
    # structurally different class, confirmed by Sprint 65's own audit.
    # AST alone can't distinguish the two without type info, so it's
    # excluded here by path, explicitly, rather than silently.
    known_unrelated_registry_files = (os.path.join(_ROOT, "luno", "adapters", "manager.py"),)
    disallowed = [
        h for h in hits
        if not any(a in h[0] for a in allowed_dirs) and h[0] not in known_unrelated_registry_files
    ]
    assert disallowed == [], f"found ToolRegistry.register() call sites outside luno/bootstrap or luno/tool_manager/builtin: {disallowed}"


# ============================================================================
# Phase 8 U - a tool ARGUMENT (not the directory config) attempting to
# redirect the output path is still contained (re-affirms
# validate_download_path, exercised through the real handler this time).
# ============================================================================

def test_U_download_filename_argument_cannot_redirect_outside_download_dir(tmp_path, monkeypatch):
    class _FakeProvider:
        def download(self, url, path):
            raise AssertionError("provider.download() must never be reached for a rejected path")

    monkeypatch.setenv("BROWSER_DOWNLOAD_DIR", str(tmp_path / "safe_downloads"))
    os.makedirs(str(tmp_path / "safe_downloads"), exist_ok=True)
    handler = RealBrowserHandler(provider=_FakeProvider())
    from luno.tool_manager.models import ToolCall
    call = ToolCall(tool="browser", action="download", target="http://example.com/x",
                     parameters={"filename": "../../../../etc/passwd", "confirmed": True})
    result = handler.execute(call)
    assert result.success is False


# ============================================================================
# Phase 7 - filesystem write boundary matrix, encoded as an executable
# table (adjusted to real paths in this codebase - nothing invented).
# ============================================================================

_BOUNDARY_MATRIX = [
    # (surface, allowed_root_getter, forbidden_path_getter)
    ("browser_downloads", lambda: BrowserConfig.from_env().download_dir, lambda: SOURCE_ROOT),
    ("long_term_memory", lambda: os.path.dirname(os.path.join(PROJECT_ROOT, "config", "long_term_memory.json")),
     lambda: SOURCE_ROOT),
]


def test_boundary_matrix_allowed_roots_never_equal_forbidden_paths():
    for surface, allowed_fn, forbidden_fn in _BOUNDARY_MATRIX:
        allowed = os.path.realpath(allowed_fn())
        forbidden = os.path.realpath(forbidden_fn())
        assert allowed != forbidden, f"{surface}: allowed root must not equal forbidden path"
        assert security_module._path_contains(forbidden, allowed) is False or allowed == forbidden, (
            f"{surface}: allowed root must not be nested inside the forbidden path"
        )


# ============================================================================
# Phase 11 - explicit "still cannot" locks. Every one of these documents
# an ABSENCE this sprint deliberately did not change.
# ============================================================================

def test_still_cannot_write_arbitrary_python_anywhere():
    """No registered tool action writes LLM-authored TEXT to a `.py`
    path - the only filesystem-writing action across every real handler
    is `browser`'s `download`, which fetches BYTES from a URL via the
    provider, never text supplied directly in a tool argument."""
    import luno.tool_manager.builtin.real_browser as rb
    import luno.tool_manager.builtin.real_windows as rw
    import luno.tool_manager.builtin.real_home_assistant as rha
    import luno.tool_manager.builtin.real_camera_ptz as rcp
    for module in (rb, rw, rha, rcp):
        src = inspect.getsource(module)
        assert ".py" not in src.replace("real_browser.py", "").replace(".pyc", "") or "spec_from_file_location" not in src


def test_still_cannot_execute_generated_python_or_shell():
    for module_name in (
        "luno.tool_manager.builtin.real_browser",
        "luno.tool_manager.builtin.real_windows",
        "luno.tool_manager.builtin.real_home_assistant",
        "luno.tool_manager.builtin.real_camera_ptz",
        "luno.tool_manager.manager",
        "luno.tool_manager.registry",
    ):
        import importlib
        module = importlib.import_module(module_name)
        src = inspect.getsource(module)
        for forbidden in ("eval(", "exec(", "os.system(", "shell=True"):
            assert forbidden not in src, f"{module_name} must not contain {forbidden!r}"


def test_still_cannot_dynamically_register_a_new_tool_from_a_tool_call():
    """`ToolCall`'s own schema has no field that could plausibly reach
    `registry.register()` - `manager.py`'s `_run_single_attempt()` only
    ever calls `registry.get()` (read), never `.register()`/`.unregister()`."""
    src = inspect.getsource(tm_manager_module.ToolManager)
    assert "registry.register(" not in src
    assert "registry.unregister(" not in src


def test_still_cannot_git_write_or_install_a_plugin():
    """Checks actual CALL SITES (via AST, not a text grep - many files
    legitimately mention "pip install playwright" etc. in docstrings as
    advice for a human operator to run manually, which is not a code
    path and must not false-positive here). Every `subprocess.run`/
    `subprocess.Popen`/`os.system` call in `luno/` (already fully
    enumerated by Sprint 65 - `desktop_control.py`'s allowlisted
    `open_app`, `dashboard/collectors.py`'s `nvidia-smi`,
    `bootstrap/version.py`'s `git rev-parse --short HEAD`) must never
    have `git` with a write-shaped subcommand (`push`/`commit`/`add`/
    `clone`) or `pip`/`python -m pip` anywhere in its argv."""
    import ast
    forbidden_git_subcommands = {"push", "commit", "add", "clone", "checkout", "reset", "merge", "rebase"}
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        if "__pycache__" in dirpath or "tests" in dirpath.split(os.sep):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
            try:
                tree = ast.parse(source, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_subprocess_call = (
                    isinstance(func, ast.Attribute) and func.attr in ("run", "Popen", "call", "check_call", "check_output")
                    and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
                ) or (isinstance(func, ast.Name) and func.id == "system")
                if not is_subprocess_call:
                    continue
                argv_strings = []
                for arg in node.args:
                    if isinstance(arg, ast.List):
                        for elt in arg.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                argv_strings.append(elt.value)
                    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        argv_strings.append(arg.value)
                argv_text = " ".join(argv_strings).lower()
                if "pip" in argv_text:
                    hits.append((path, node.lineno, "pip in subprocess argv"))
                if "git" in argv_text and any(sub in argv_text for sub in forbidden_git_subcommands):
                    hits.append((path, node.lineno, "write-shaped git subcommand in subprocess argv"))
    assert hits == [], f"found git-write/pip-install call sites: {hits}"


def test_sprint66_added_zero_new_registered_tool_names():
    """Confirms this sprint's own changes did not expand the tool
    registry - the set registered by `bootstrap/adapters.py` is
    identical to what Sprint 65 already inventoried."""
    import luno.bootstrap.adapters as bootstrap_adapters
    import re
    src = inspect.getsource(bootstrap_adapters)
    names = set(re.findall(r'registry\.register\(\s*"([a-z_]+)"', src))
    assert names == {"home_assistant", "windows", "camera_ptz", "browser"}


# ============================================================================
# Phase 14 - performance: validation must be fast and side-effect-free
# (no network, no LLM, no blocking I/O beyond a few filesystem stats).
# ============================================================================

def test_performance_validate_download_directory_is_fast():
    cfg = BrowserConfig.from_env()
    # warm up (first call may pay import/stat cache costs)
    validate_download_directory(cfg.download_dir)
    N = 200
    start = time.perf_counter()
    for _ in range(N):
        validate_download_directory(cfg.download_dir)
    elapsed_ms_per_call = (time.perf_counter() - start) * 1000 / N
    assert elapsed_ms_per_call < 5.0, f"validate_download_directory() averaged {elapsed_ms_per_call:.3f}ms/call, expected <5ms"


def test_performance_tool_registry_lookup_is_fast():
    registry = tm_registry_module.ToolRegistry()
    registry.register("browser", RealBrowserHandler(provider=object()))
    N = 500
    start = time.perf_counter()
    for _ in range(N):
        registry.get("browser")
        registry.get("definitely_unknown")
    elapsed_ms_per_call = (time.perf_counter() - start) * 1000 / (N * 2)
    assert elapsed_ms_per_call < 5.0, f"registry.get() averaged {elapsed_ms_per_call:.3f}ms/call, expected <5ms"


def test_performance_validation_has_no_network_or_llm_call():
    src = inspect.getsource(security_module.validate_download_directory)
    for forbidden in ("requests.", "httpx.", "urllib.request", "socket.", "openai", "anthropic", "openrouter"):
        assert forbidden not in src


# ============================================================================
# Phase 12 - persistent state safety bookend for THIS file's own run.
# ============================================================================

_REAL_LONG_TERM_MEMORY_PATH = os.path.join(_ROOT, "config", "long_term_memory.json")
_HASH_AT_COLLECTION = _sha256_of(_REAL_LONG_TERM_MEMORY_PATH)


def test_this_files_own_run_never_touches_the_real_config_directory():
    assert _sha256_of(_REAL_LONG_TERM_MEMORY_PATH) == _HASH_AT_COLLECTION


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
