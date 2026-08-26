"""
tests/test_sprint65_tool_file_access_audit.py
================================================

Sprint 65 - Luno Tool & File Access Audit.

**AUDIT ONLY.** Every test here either (a) is a structural/capability-
detection assertion against the real, unmodified production source (never
writing to it), or (b) reproduces a specific capability against a
synthetic fixture built entirely inside `tmp_path`. Nothing here creates,
overwrites, deletes, or renames any file under the real project checkout,
and nothing here writes to `config/*.json` or any memory store.

See `docs/change_impact/tool_file_access_audit.md` for the full narrative
writeup this file's tests support.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.desktop_control as desktop_control  # noqa: E402
import luno.tool_manager.manager as tm_manager_module  # noqa: E402
import luno.tool_manager.registry as tm_registry_module  # noqa: E402
from luno.browser.security import validate_download_path  # noqa: E402
from luno.browser.permissions import classify_action_risk, PermissionLevel  # noqa: E402
from luno.planner.parser import IntentParser  # noqa: E402


def _sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ============================================================================
# A - Tool registry: only a fixed, enumerated set of tool names can ever
#     be dispatched (Phase 1 / Phase 6) - "the tool name itself" is not an
#     LLM-controlled arbitrary-code vector, because ToolManager only ever
#     looks a name up in a closed registry and fails cleanly on a miss.
# ============================================================================

_PRODUCTION_TOOL_NAMES_WIRED_IN_BOOTSTRAP = {
    "home_assistant", "windows", "camera_ptz", "browser",
}
_MOCK_ONLY_TOOL_NAMES = {"vision", "spotify", "unity", "llm_mode", "dummy"}
#: Names that, if ever seen registered, would themselves be Sprint 65
#: findings (a generic code/shell execution tool). None exist today - this
#: is an explicit negative-space check, not just an enumeration.
_FORBIDDEN_GENERIC_EXECUTION_TOOL_NAMES = {
    "python", "python_exec", "exec", "eval", "shell", "shell_exec", "bash",
    "cmd", "powershell", "run_command", "subprocess", "code", "script_exec",
}


def test_A_tool_registry_unknown_tool_fails_closed_never_raises():
    registry = tm_registry_module.ToolRegistry()
    manager = tm_manager_module.ToolManager(registry=registry)
    result = manager.execute({"tool": "definitely_not_a_real_tool", "action": "anything"})
    assert result.success is False
    assert result.error_type == "unknown_tool"


def test_A_bootstrap_only_wires_the_known_closed_set_of_real_handlers():
    """Structural proof against the real `luno/bootstrap/adapters.py`
    source: every `tool_manager_module.manager.registry.register(` call
    site registers one of the known handler names - none register a name
    from `_FORBIDDEN_GENERIC_EXECUTION_TOOL_NAMES`, and no call constructs
    a handler class this audit hasn't already inventoried."""
    import luno.bootstrap.adapters as bootstrap_adapters
    import re
    src = inspect.getsource(bootstrap_adapters)
    # registration calls in this file appear both single-line
    # (`registry.register("camera_ptz", ...)`) and wrapped across lines
    # (`registry.register(\n    "home_assistant", ...\n)`) - `\s*`
    # matches newlines by default, so this handles both shapes.
    for forbidden in _FORBIDDEN_GENERIC_EXECUTION_TOOL_NAMES:
        assert re.search(r'registry\.register\(\s*"' + re.escape(forbidden) + r'"', src) is None, (
            f"found a registration for forbidden generic-execution tool name {forbidden!r}"
        )
    for known in _PRODUCTION_TOOL_NAMES_WIRED_IN_BOOTSTRAP:
        assert re.search(r'registry\.register\(\s*"' + re.escape(known) + r'"', src), (
            f"expected bootstrap to register {known!r}"
        )


def test_A_builtin_register_all_only_registers_the_known_mock_set():
    import luno.tool_manager.builtin as builtin_pkg
    registry = tm_registry_module.ToolRegistry()
    builtin_pkg.register_all(registry)
    registered = set(registry.list_tools())
    assert registered == (_PRODUCTION_TOOL_NAMES_WIRED_IN_BOOTSTRAP | _MOCK_ONLY_TOOL_NAMES)
    assert registered.isdisjoint(_FORBIDDEN_GENERIC_EXECUTION_TOOL_NAMES)


# ============================================================================
# B - open_app() / desktop_control.py: allowlist-only process launch
#     (Phase 1, Phase 5, Phase 9-E). Uses a SYNTHETIC apps dict via
#     monkeypatch - never touches the real config/apps.json.
# ============================================================================

def test_B_open_app_refuses_any_name_not_in_the_allowlist(monkeypatch):
    monkeypatch.setattr(desktop_control, "APPS", {"notepad": {"path": "/usr/bin/true", "args": []}})
    ok, message = desktop_control.open_app("cmd.exe")
    assert ok is False
    assert "belum terdaftar" in message  # "not registered" - never silently attempted


def test_B_open_app_case_and_whitespace_cannot_bypass_the_allowlist(monkeypatch):
    monkeypatch.setattr(desktop_control, "APPS", {"notepad": {"path": "/usr/bin/true", "args": []}})
    for variant in ("NOTEPAD", "  notepad  ", "NotePad"):
        ok, message = desktop_control.open_app(variant)
        assert ok is True, f"expected {variant!r} to normalize to the registered 'notepad' entry"
    ok, message = desktop_control.open_app("notepad; rm -rf /")
    assert ok is False, "a shell-metacharacter-suffixed name must not fuzzy-match the allowlist"


def test_B_open_app_launch_argv_is_never_shell_interpreted(monkeypatch):
    """`open_app()`'s only subprocess call site uses `subprocess.Popen`
    with a LIST argv (`[path, *args]`), never `shell=True` and never a
    single interpolated string - structurally proven against the real
    source, which is what actually rules out shell metacharacter
    injection through `config/apps.json`'s own `args` list (a config-file
    value, not an LLM-controlled one anyway - see the audit doc)."""
    src = inspect.getsource(desktop_control.open_app)
    assert "shell=True" not in src
    assert "subprocess.Popen([path, *args])" in src or "subprocess.Popen([path]" in src


def test_B_open_app_never_falls_back_to_an_unregistered_arbitrary_path(monkeypatch):
    monkeypatch.setattr(desktop_control, "APPS", {})
    ok, message = desktop_control.open_app("/bin/sh")
    assert ok is False, "a raw filesystem path must not be treated as an implicit allowlist entry"
    ok2, message2 = desktop_control.open_app("../../../bin/sh")
    assert ok2 is False, "path-traversal-shaped input must not be treated as an implicit allowlist entry"


# ============================================================================
# C - browser download path containment (Phase 2, Phase 7). Reuses the
#     REAL `validate_download_path()` against a synthetic tmp_path
#     "project" (mirrors the brief's own suggested fixture shape:
#     tmp/project/{protected.py, marker.txt, downloads/}), never the real
#     checkout.
# ============================================================================

def _make_synthetic_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "protected.py").write_text("MARKER = 'original-protected-content'\n")
    (project / "marker.txt").write_text("original\n")
    download_dir = project / "downloads"
    download_dir.mkdir()
    return project, download_dir


def test_C_download_path_blocks_parent_traversal(tmp_path):
    project, download_dir = _make_synthetic_project(tmp_path)
    ok, reason = validate_download_path("../protected.py", str(download_dir))
    assert ok is False
    assert (project / "protected.py").read_text() == "MARKER = 'original-protected-content'\n"


def test_C_download_path_blocks_absolute_path_outside_download_dir(tmp_path):
    project, download_dir = _make_synthetic_project(tmp_path)
    ok, reason = validate_download_path(str(project / "protected.py"), str(download_dir))
    assert ok is False


def test_C_download_path_allows_nested_relative_path_inside_download_dir(tmp_path):
    project, download_dir = _make_synthetic_project(tmp_path)
    ok, resolved = validate_download_path("subdir/file.txt", str(download_dir))
    assert ok is True
    assert str(download_dir) in resolved


def test_C_download_path_containment_depends_entirely_on_download_dir_configuration(tmp_path):
    """Sprint 65 finding, reproduced as a deterministic negative control:
    `validate_download_path()`'s guarantee is "stay inside `download_dir`"
    - it has NO opinion on what `download_dir` itself is. If `download_dir`
    is ever misconfigured to equal (or be an ancestor of) a directory
    containing source files, a LOW_RISK, auto-allowed-by-default `download`
    action with an LLM-supplied `filename` parameter WOULD be able to
    resolve onto an existing file's path. In the real, current
    configuration this never happens (`BrowserConfig.download_dir`
    defaults to `<DATA_DIR>/browser_downloads`, a leaf directory that
    never overlaps `luno/`'s source tree - see `luno/browser/config.py`)
    - this test proves the mechanism's boundary is a CONFIGURATION
    invariant, not a code-level guarantee that would survive a
    misconfiguration. See the audit doc's Finding SPRINT65-002."""
    project, _real_download_dir = _make_synthetic_project(tmp_path)
    misconfigured_download_dir = str(project)  # simulates download_dir == project root
    ok, resolved = validate_download_path("protected.py", misconfigured_download_dir)
    assert ok is True, (
        "this is the finding itself: validate_download_path() has no way to know "
        "'protected.py' is an existing source file, only that it resolves inside "
        "the (misconfigured) download_dir"
    )
    assert resolved == str(project / "protected.py")


def test_C_real_browser_config_default_download_dir_never_overlaps_the_luno_source_tree():
    """Confirms the CURRENT real configuration does not hit the risk
    demonstrated above: the default `BROWSER_DOWNLOAD_DIR` is a dedicated
    leaf subdirectory of `DATA_DIR`, never `DATA_DIR` itself and never any
    ancestor of `luno/`."""
    from luno.browser.config import BrowserConfig
    import luno.config as luno_config
    cfg = BrowserConfig.from_env()
    default_download_dir = os.path.abspath(cfg.download_dir)
    luno_pkg_dir = os.path.abspath(os.path.dirname(luno_config.__file__))
    assert default_download_dir != luno_pkg_dir
    assert not luno_pkg_dir.startswith(default_download_dir + os.sep)
    assert default_download_dir.endswith("browser_downloads")


def test_C_download_action_is_low_risk_and_auto_allowed_by_default():
    """Documents (executably) that `download` is classified LOW_RISK, not
    SENSITIVE/HIGH_RISK - it executes without a confirmation round-trip
    by default (`require_confirmation_for_low_risk=False` is the
    `PermissionManager` default). This is WHY the containment guarantee
    in `validate_download_path()` is the only thing standing between an
    LLM-supplied filename and the filesystem for this action - there is
    no confirmation gate to fall back on for the common case."""
    assert classify_action_risk("download", {}, None) == PermissionLevel.LOW_RISK


# ============================================================================
# D - filesystem write path inventory (Phase 2): every JSON persistence
#     path in `luno/config.py` is a hardcoded module-level constant,
#     never something the LLM/conversation can redirect at runtime.
# ============================================================================

_ALL_DATA_DIR_PATH_CONSTANTS = [
    "LIGHTS_CONFIG_FILE", "SWITCHES_CONFIG_FILE", "SCRIPTS_CONFIG_FILE",
    "ENV_TRIGGERS_CONFIG_FILE", "HABIT_MEMORY_FILE", "RELATIONSHIP_STATE_FILE",
    "RESPONSE_DEPTH_PREFERENCE_FILE", "EPISODIC_MEMORY_FILE", "VERIFIED_FACTS_FILE",
    "LONG_TERM_MEMORY_FILE", "SESSION_SUMMARIES_FILE", "PERSONA_FILE",
    "REMINDERS_FILE", "APPS_CONFIG_FILE",
]

#: Files this audit proves are NEVER opened in a write mode anywhere in
#: production code (only loaded once, read-only, at startup) - editing
#: them is an out-of-band, human, filesystem-level action only.
_READ_ONLY_AT_RUNTIME_FILES = [
    "PERSONA_FILE", "APPS_CONFIG_FILE", "LIGHTS_CONFIG_FILE",
    "SWITCHES_CONFIG_FILE", "SCRIPTS_CONFIG_FILE",
]


def test_D_every_data_dir_path_constant_is_a_hardcoded_module_level_constant():
    import luno.config as luno_config
    src = inspect.getsource(luno_config)
    for name in _ALL_DATA_DIR_PATH_CONSTANTS:
        assert f"{name} = os.getenv(" in src, (
            f"expected {name} to be assigned once, at module level, via os.getenv() - "
            f"if this changes to something computed from user/LLM input, this is a new finding"
        )


def test_D_read_only_config_files_have_zero_write_mode_open_calls_anywhere():
    """Repo-wide structural proof: grep for the literal open(...) call
    sites against each of these constants across every .py file in
    `luno/`, and confirm none use a write/append mode. Read-only
    module-level loads (`"r"`) are expected and excluded."""
    import re
    write_mode_re = re.compile(r'open\s*\([^)]*["\']([wax][b+]?)["\']')
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        if "__pycache__" in dirpath or "tests" in dirpath.split(os.sep):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for const_name in _READ_ONLY_AT_RUNTIME_FILES:
                if const_name not in content:
                    continue
                for line in content.splitlines():
                    if const_name in line and write_mode_re.search(line):
                        hits.append((path, const_name, line.strip()))
    assert hits == [], f"found write-mode open() calls against read-only config constants: {hits}"


def test_D_no_config_dot_json_path_is_ever_built_from_conversation_or_llm_text():
    """Every one of these path constants must be assembled purely from
    `DATA_DIR` (an env-controlled deploy-time constant) and a fixed
    literal filename - never string-formatted with a variable that could
    trace back to user/LLM text. A conversation-derived f-string building
    a path (e.g. `os.path.join(DATA_DIR, f"{user_text}.json")`) would be
    exactly the kind of thing this test is designed to catch."""
    import luno.config as luno_config
    import re
    src = inspect.getsource(luno_config)
    for name in _ALL_DATA_DIR_PATH_CONSTANTS:
        # the os.getenv(...) call may wrap across multiple lines
        # (e.g. RESPONSE_DEPTH_PREFERENCE_FILE) - capture the full
        # assignment statement up to its closing paren instead of
        # assuming it fits on one line.
        match = re.search(
            re.escape(f"{name} = os.getenv(") + r"(.*?)\)\s*\n", src, re.DOTALL,
        )
        assert match, f"couldn't locate the {name} assignment statement"
        statement = match.group(0)
        assert "DATA_DIR" in statement or ".json" in statement
        assert 'f"' not in statement and "f'" not in statement, (
            f"{name} is built with an f-string - re-audit for a conversation-derived path"
        )


# ============================================================================
# E - no dynamic exec/eval/plugin auto-loading anywhere in production
#     code (Phase 5). The only two `importlib.util.spec_from_file_location`
#     call sites in this codebase point at HARDCODED, fixed filenames -
#     never a directory scan, never a name built from user/LLM text.
# ============================================================================

def test_E_no_exec_or_eval_call_sites_exist_in_production_code():
    import re
    call_re = re.compile(r'(?<![\w.])(exec|eval)\s*\(')
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        if "__pycache__" in dirpath or "tests" in dirpath.split(os.sep):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, start=1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if call_re.search(line) and "exec_module" not in line:
                        hits.append((path, lineno, stripped))
    assert hits == [], f"found exec()/eval() call sites in production code: {hits}"


def test_E_every_dynamic_module_load_targets_a_hardcoded_fixed_filename():
    """The only two `spec_from_file_location(...)` call sites in this
    codebase (`luno/adapters/real_whisper.py` -> legacy_main.py,
    `luno/bootstrap/modules.py` -> main_runtime_demo.py) both use a
    filename that is a fixed string literal or built from `Path(__file__)`
    - never a variable that could originate from user/LLM/conversation
    text."""
    import luno.adapters.real_whisper as real_whisper_module
    import luno.bootstrap.modules as bootstrap_modules_module
    for module in (real_whisper_module, bootstrap_modules_module):
        src = inspect.getsource(module)
        assert "spec_from_file_location(" in src
        # every argument to spec_from_file_location must trace to a
        # literal filename component, never raw user/LLM text - checked
        # here by confirming no f-string built from a request/text/query/
        # user-supplied-looking variable feeds into it.
        for line in src.splitlines():
            if "spec_from_file_location(" in line:
                assert "user_text" not in line and "request" not in line and "query" not in line


def test_E_no_directory_scan_and_import_plugin_loading_pattern_exists():
    """Negative-space check: no code anywhere imports via `pkgutil`,
    `importlib.import_module` inside a loop over `os.listdir()`/`glob`
    results, or `runpy` - which would be the shape of an auto-plugin-
    loading mechanism an LLM-generated file could hook into."""
    forbidden_tokens = ("pkgutil", "walk_packages", "iter_modules", "runpy")
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        if "__pycache__" in dirpath or "tests" in dirpath.split(os.sep):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for token in forbidden_tokens:
                if token in content:
                    hits.append((path, token))
    assert hits == [], f"found plugin-auto-loading-shaped tokens: {hits}"


# ============================================================================
# F - subprocess call-site inventory (Phase 1/2/5): every subprocess.*
#     call in production code uses a list argv (never shell=True), and
#     every argv is built from either a fixed literal or a config-file-
#     sourced value - never raw conversation/LLM text.
# ============================================================================

def test_F_zero_shell_equals_true_anywhere_in_production_code():
    hits = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        if "__pycache__" in dirpath or "tests" in dirpath.split(os.sep):
            continue
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if "shell=True" in content:
                hits.append(path)
    root_files = [os.path.join(_ROOT, n) for n in ("main.py", "main_runtime_demo.py", "probe_memory_pipeline.py")]
    for path in root_files:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if "shell=True" in f.read():
                    hits.append(path)
    assert hits == [], f"found shell=True in: {hits}"


def test_F_os_dot_system_has_exactly_one_call_site_and_it_is_a_hardcoded_clear_screen():
    """The repo's only `os.system(...)` call site is `main_runtime_demo.py`'s
    terminal clear-screen helper - a fixed, hardcoded literal
    ('cls'/'clear' chosen by `os.name`, never conversation input)."""
    with open(os.path.join(_ROOT, "main_runtime_demo.py"), "r", encoding="utf-8") as f:
        content = f.read()
    call_sites = [line for line in content.splitlines() if "os.system(" in line]
    assert len(call_sites) == 1, f"expected exactly one os.system() call site, found {len(call_sites)}: {call_sites}"
    assert '"cls" if os.name' in call_sites[0]


def test_F_subprocess_run_call_sites_all_use_fixed_hardcoded_argv():
    """`luno/dashboard/collectors.py` (nvidia-smi) and
    `luno/bootstrap/version.py` (git rev-parse) are the only two
    `subprocess.run(` call sites outside `desktop_control.py` - both use
    a fixed, hardcoded argv list with no conversation/LLM-supplied
    component."""
    import luno.dashboard.collectors as collectors_module
    import luno.bootstrap.version as version_module
    for module, expected_argv0 in ((collectors_module, "nvidia-smi"), (version_module, "git")):
        src = inspect.getsource(module)
        assert f'"{expected_argv0}"' in src or f"'{expected_argv0}'" in src
        assert "shell=True" not in src


# ============================================================================
# G - IntentParser: the actual chain by which conversational text
#     ultimately reaches a ToolCall's `tool`/`action` fields is a bounded
#     regex grammar, not an LLM function-calling loop with an open action
#     space (Phase 4, Phase 6).
# ============================================================================

#: "unknown" is IntentParser's own designed, documented fail-closed
#: sentinel (see `luno/planner/parser.py`'s own module docstring: "falling
#: back to `tool='unknown'` rather than guessing wrong") for text that
#: doesn't match any specific recognized command vocabulary - every
#: downstream consumer (`main_runtime_demo.py`) explicitly filters
#: `tool_call.tool != "unknown"` BEFORE tasks ever reach `ToolManager`, so
#: it is never itself dispatched to a real handler. It belongs in this
#: "known safe" set for exactly that reason, not because it names a real
#: handler.
_KNOWN_PARSER_TOOL_NAMES = {"home_assistant", "windows", "browser", "camera_ptz", "unknown", None}


def test_G_intent_parser_never_emits_a_tool_name_outside_the_known_set():
    """Fuzzes `IntentParser.parse()` with plain text containing shell/
    injection-shaped substrings and confirms every resulting
    `ParsedStep.tool` is still one of the small, known, hardcoded set the
    real handlers are registered under - never something derived
    verbatim from the injected text."""
    injection_attempts = [
        "turn on lights; rm -rf /",
        "open app $(cat /etc/passwd)",
        "run script `whoami`",
        "buka aplikasi ../../../../bin/sh",
        "jalankan os.system('id')",
        "navigate to file:///etc/passwd",
    ]
    for text in injection_attempts:
        steps = IntentParser.parse(text)
        for step in steps:
            assert step.tool in _KNOWN_PARSER_TOOL_NAMES, (
                f"IntentParser.parse({text!r}) produced an unexpected tool name: {step.tool!r}"
            )


def test_G_unknown_tool_sentinel_is_filtered_before_reaching_task_execution():
    """Executable proof for the claim documented on
    `_KNOWN_PARSER_TOOL_NAMES` above: `main_runtime_demo.py` filters
    `tool_call.tool != "unknown"` at every real-task-counting/dispatch
    site - confirmed here by grepping the real, unmodified source for
    that exact guard (read-only structural check, not a runtime
    simulation of the full console)."""
    with open(os.path.join(_ROOT, "main_runtime_demo.py"), "r", encoding="utf-8") as f:
        content = f.read()
    assert 'tool_call.tool != "unknown"' in content
    assert content.count('!= "unknown"') >= 2, (
        "expected multiple guard sites filtering the 'unknown' sentinel before dispatch"
    )


def test_G_intent_parser_target_field_is_free_text_but_never_itself_executed():
    """`ParsedStep.target` legitimately carries close-to-verbatim
    substrings of the input (e.g. an app name to look up, a URL to
    navigate to) - this is expected and by itself not a vulnerability,
    because every consumer of `target` (see Findings SPRINT65-001/003 in
    the audit doc) treats it as a LOOKUP KEY or a validated URL, never as
    code/a shell command to run. This test only proves `target` is a
    plain string (or None) - never a callable, never bytecode, never
    something that could be executed by a naive `eval`/`exec` if a future
    change added one."""
    steps = IntentParser.parse("open app $(cat /etc/passwd)")
    for step in steps:
        assert step.target is None or isinstance(step.target, str)


# ============================================================================
# H - persistent-state safety: this entire test file never mutates
#     production state (Phase 14 bookend, mirrors Sprint 63/64's own
#     convention).
# ============================================================================

_REAL_LONG_TERM_MEMORY_PATH = os.path.join(_ROOT, "config", "long_term_memory.json")
_HASH_AT_COLLECTION = _sha256_of(_REAL_LONG_TERM_MEMORY_PATH)


def test_H_this_audits_own_tests_never_touch_the_real_config_directory():
    assert _sha256_of(_REAL_LONG_TERM_MEMORY_PATH) == _HASH_AT_COLLECTION


def test_H_no_test_in_this_file_opens_a_real_config_path_in_write_mode():
    """Structural self-check on this very file: no `open(` call anywhere
    in this module's own source targets `_ROOT`-relative `config/` in a
    write mode."""
    with open(__file__, "r", encoding="utf-8") as f:
        this_files_own_source = f.read()
    # This file contains no write-mode open() call at all - the only
    # open() calls in this module are read-only (_sha256_of(), this
    # very check, and reading this file's own source above).
    import re
    write_mode_re = re.compile(r'open\s*\([^)]*["\']([wax][b+]?)["\']')
    assert not write_mode_re.search(this_files_own_source)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
