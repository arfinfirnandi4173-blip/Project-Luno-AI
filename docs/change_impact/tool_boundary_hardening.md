# Sprint 66 — Tool Boundary Hardening

**Type:** Security hardening, not a new audit. Directly addresses the two
findings Sprint 65 left open. **Prerequisite satisfied:** Sprint 65
(`docs/change_impact/tool_file_access_audit.md`) was complete before this
sprint began, and its evidence — not a re-audit from scratch — is what
this sprint builds on. Explicitly out of scope, per the brief's own
prohibition: shell execution, arbitrary Python execution, arbitrary
filesystem write, source-code modification, git write, plugin
installation, dynamic tool registration. None of these were added. All
remain absent, and Phase 11 now locks that absence down with tests.

---

## Phase 0 — Evidence read before any change

Read before writing any code: `docs/change_impact/tool_file_access_audit.md`
in full, `ARCHITECTURE_GUARD.md`'s Sprint 65 section (§66),
`tests/test_sprint65_tool_file_access_audit.py`, `luno/browser/security.py`,
`luno/browser/config.py`, `luno/browser/permissions.py`,
`luno/tool_manager/builtin/real_browser.py`, `luno/tool_manager/manager.py`,
`luno/tool_manager/registry.py`, `luno/tool_manager/handler.py`,
`luno/bootstrap/adapters.py`, `luno/adapters/manager.py`. Source code was
treated as authority over docs throughout, per the brief's own rule.

## Phase 1 — The download chain, traced end to end

`LLM → browser tool call (action="download") → RealBrowserHandler._dispatch()
→ destination = params["filename"] or _filename_from_url(url) →
validate_download_path(destination, cfg.download_dir) → p.download(url, resolved)
→ BrowserProvider (Playwright) writes the file`.

Answers to the brief's 9 questions about `download_dir`'s origin:

1. **Origin:** `BrowserConfig.from_env()` reads `os.getenv("BROWSER_DOWNLOAD_DIR", "").strip()`, falling back to `os.path.join(DATA_DIR, "browser_downloads")` where `DATA_DIR = os.getenv("DATA_DIR", "config")` (`luno/browser/config.py`).
2. **Config or LLM?** Environment variable / config only. No tool call, LLM output, or conversation text can set or influence `download_dir` — the LLM only ever supplies `filename`/`url` per-call, never the directory.
3. **Tool argument?** No — `download_dir` is never a `ToolCall.parameters` key; only `filename` and `url` are, and both are validated against the fixed `download_dir`, not the other way around.
4. **User input at runtime?** No interactive prompt sets it; only process environment at startup (or at each `from_env()` re-read, since this package's convention is "reloadable without a restart").
5. **External service?** No — purely local environment/config, not fetched from Home Assistant or any network service.
6. **Absolute or relative?** May be either — `os.getenv()` returns whatever string is configured; the default (`config/browser_downloads`) is relative to the process's working directory.
7. **How resolved?** Previously: `os.path.abspath()` only (in `validate_download_path()`). Now: `_resolve_for_comparison()` — `os.path.normcase(os.path.realpath(os.path.abspath(path)))` — for both the new directory-level guard and the upgraded per-file guard.
8. **Symlink/`..`/case/UNC/drive-letter/junction/reparse-point bypass?** `os.path.realpath()` resolves `.`/`..` and, since Python 3.8, symlinks and Windows junctions/reparse points (via `GetFinalPathNameByHandleW` on Windows). `os.path.normcase()` lowercases and normalizes separators on Windows (no-op on POSIX), closing the case-variation gap. Different-drive-letter paths (`C:\...` vs `D:\...`) make `os.path.commonpath()` raise `ValueError`, which `_path_contains()` treats as "not contained" — a safe (non-permissive) default. UNC paths follow the same `realpath`/`normcase` handling; no separate UNC-specific code path exists or was added, since none was needed for these checks to behave correctly.
9. **Does download_dir ever reach the filesystem sink unvalidated?** No — as of this sprint, every path to `p.download()` passes through `validate_download_directory()` (directory-level) followed by `validate_download_path()` (file-level) first, both using the same canonical-resolution primitive.

## Phase 2 — Boundary definitions and the invariant

- **SOURCE_ROOT** — the `luno/` package directory. Computed as `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))` from `luno/browser/security.py` (two levels up from `luno/browser/`).
- **PROJECT_ROOT** — the repository root, one level above `SOURCE_ROOT`.
- **DOWNLOAD_ROOT** — whatever `BrowserConfig.from_env().download_dir` currently resolves to; not a fixed constant, since it's operator-configurable.
- **CRITICAL_PATHS** — every `luno.config` module attribute ending in `_FILE` whose value is a string (enumerated dynamically — 14 found: `APPS_CONFIG_FILE`, `ENV_TRIGGERS_CONFIG_FILE`, `EPISODIC_MEMORY_FILE`, `HABIT_MEMORY_FILE`, `LIGHTS_CONFIG_FILE`, `LONG_TERM_MEMORY_FILE`, `PERSONA_FILE`, `RELATIONSHIP_STATE_FILE`, `REMINDERS_FILE`, `RESPONSE_DEPTH_PREFERENCE_FILE`, `SCRIPTS_CONFIG_FILE`, `SESSION_SUMMARIES_FILE`, `SWITCHES_CONFIG_FILE`, `VERIFIED_FACTS_FILE`), resolved to absolute paths relative to `PROJECT_ROOT`, plus a fixed set of root-level files that aren't `luno.config` attributes (`main.py`, `main_runtime_demo.py`, `probe_memory_pipeline.py`, `ARCHITECTURE_GUARD.md`, `requirements.txt`, `.env`).

**The invariant actually implemented** (see `validate_download_directory()`'s
own docstring in `luno/browser/security.py` for the code-level statement):
`DOWNLOAD_ROOT` must never equal or contain `SOURCE_ROOT`; must never be
contained by `SOURCE_ROOT`; must never equal or be an ancestor of
`PROJECT_ROOT`; and must never equal or contain any individual
`CRITICAL_PATHS` file.

**Why `DOWNLOAD_ROOT` is allowed to nest under `PROJECT_ROOT`:** a literal
reading of the brief's Phase 3 wording ("reject if path is inside
source/project root") would reject the current, working, correct default
(`config/browser_downloads`, which is nested under `PROJECT_ROOT`) — that
would directly violate Phase 2's own explicit instruction ("Jangan membuat
browser tidak berguna hanya demi security") and Phase 9's negative-control
requirement that legitimate behavior keep working. Reconciling the two:
this sprint reads "source/project root" as **SOURCE_ROOT** (the actual
code that could be tampered with) plus the **individually enumerated
CRITICAL_PATHS** (the actual files that matter) — not literally every byte
under `PROJECT_ROOT`, most of which is non-critical (docs, config for
non-code data, this very test file's own directory). `PROJECT_ROOT`
containment is still enforced for the *ancestor* direction (a
`download_dir` that contains `PROJECT_ROOT` — e.g. pointing at `/` or a
parent of the repo — is rejected), since that direction has no legitimate
use case and only expands blast radius. This is a documented judgment
call, not an ambiguity left unresolved.

## Phase 3 — `validate_download_directory()`

New function in `luno/browser/security.py`. Signature:
`validate_download_directory(download_dir: str) -> tuple[bool, str]`.
Implementation requirements satisfied:

- Resolves to canonical form via `_resolve_for_comparison()` before any comparison.
- Rejects exact equality with `SOURCE_ROOT` and `PROJECT_ROOT`.
- Rejects `download_dir` containing `SOURCE_ROOT` or being contained by it (both directions checked).
- Rejects `download_dir` being an ancestor of `PROJECT_ROOT` (covers "project root is inside download root").
- Rejects `download_dir` equaling or containing any single `CRITICAL_PATHS` entry.
- Path traversal (`../../etc`) and absolute-path escapes are neutralized by `realpath()` before comparison — there is no separate traversal-detection step because canonicalization removes the distinction between "looks like traversal" and "resolves outside the boundary."
- Windows path semantics: `normcase()` folds case and separator differences; `realpath()` resolves drive-relative and UNC forms the same way it resolves POSIX paths.
- Symlinks/junctions/reparse points: resolved by `realpath()` wherever the underlying platform supports it (verified via synthetic symlink trees in Phase 8's tests; junction/reparse-point behavior specifically is a structural proof only, since this sandbox is Linux — see Known Limitations).
- Comparisons use `_path_contains()` (`os.path.commonpath()`-based), never `str.startswith()` — the brief explicitly names `startswith()` as insufficient (it would treat `/a/b-evil` as "inside" `/a/b`), and `_path_contains()` does not have that failure mode.

## Phase 4 — Fail-closed validation timing

`RealBrowserHandler.__init__()` (`luno/tool_manager/builtin/real_browser.py`)
validates `BrowserConfig.from_env().download_dir` at construction time —
functionally "at startup," since this constructor only ever runs once,
during `luno/bootstrap/adapters.py::_register_real_browser_handler()`'s
real-handler registration pass. On failure it raises `ValueError` with the
configured path, the expected boundary (source package + critical files),
and the specific rejection reason — never environment variable dumps or
credentials, since the message is built entirely from the path strings
already being validated, not from `os.environ`. This raise needed no new
plumbing: `_register_real_browser_handler()` already wraps handler
construction in a try/except that falls back to "stay mocked" on any
registration failure, a pre-existing pattern this sprint reused rather
than duplicated.

## Phase 5 — Defense in depth

Two independent layers, not one:

1. **Construction-time (startup-equivalent):** `RealBrowserHandler.__init__()`, described above.
2. **Per-call (runtime):** `RealBrowserHandler._dispatch()`'s `"download"` branch re-validates `cfg.download_dir` via `validate_download_directory()` on every single download call, before the pre-existing per-file `validate_download_path()` check. This is necessary, not redundant: `BrowserConfig.from_env()` is documented as "reloadable without a restart" (reads `os.getenv()` fresh every call per this package's own convention), so a configuration change made after the handler was constructed would bypass a startup-only check entirely.

## Phase 6 — Tool registry immutability

**Finding: already safe by construction. No registry code was changed.**

- No handler `__init__` (checked all four real handlers: `real_browser.py`, `real_windows.py`, `real_home_assistant.py`, `real_camera_ptz.py`) accepts or stores a `ToolRegistry` reference — a handler has no way to reach the registry that holds it.
- `ToolRegistry.register()`/`.unregister()` are called only from `luno/bootstrap/adapters.py` and `luno/tool_manager/builtin/__init__.py::register_all()` — confirmed via an AST-based structural test (not text search) that parses every `luno/**/*.py` file, walks for `ast.Call` nodes shaped like `<name-or-attribute ending in "registry">.register(...)`, and asserts the call sites are only in the two expected files. `luno/adapters/manager.py:103`'s `self.registry.register(adapter, cfg)` is syntactically identical but belongs to a structurally different, unrelated `AdapterRegistry` class (confirmed by reading that file) — excluded explicitly, by name, with an inline comment, not silently.
- No runtime code path — including `ToolManager._run_single_attempt()`, the only place a `ToolCall` is dispatched — ever calls `.register()`/`.unregister()`; it only calls `.get()` (read-only).
- `ToolRegistry.get()`'s entire implementation is `self._handlers.get(name)` — a bare `dict.get()`. No `importlib`, `getattr()`-on-a-string, `eval()`, or `exec()` anywhere in its path, so a tool name can never become an arbitrary Python import or executable path.
- Unknown tool names and unknown actions both fail closed (`error_type="unknown_tool"` / a `validate()` rejection), never falling through to any default-execute behavior.

Per the brief's own instruction ("jangan mengubah registry architecture
jika existing implementation sudah aman — tambahkan tests saja"), this
sprint added regression tests only (Phase 8's P/Q/R/S/T cases, Phase 11's
dynamic-registration lock test) and made zero changes to
`luno/tool_manager/registry.py` or `luno/tool_manager/manager.py`.

## Phase 7 — Filesystem write boundary matrix

| Surface | Allowed | Forbidden |
|---|---|---|
| `browser` tool, `download` action | Writing inside a `download_dir` that passes `validate_download_directory()` (default: `config/browser_downloads`) | Writing to `SOURCE_ROOT` (`luno/`), any `CRITICAL_PATHS` file, or a `download_dir` that fails validation (fails closed — handler refuses to even construct, or the call is refused at dispatch) |
| Memory/preference writers (`EPISODIC_MEMORY_FILE`, `HABIT_MEMORY_FILE`, `LONG_TERM_MEMORY_FILE`, `SESSION_SUMMARIES_FILE`, `REMINDERS_FILE`, `VERIFIED_FACTS_FILE`, `RESPONSE_DEPTH_PREFERENCE_FILE`, `RELATIONSHIP_STATE_FILE`) | Writing their own single, hardcoded-path JSON file (unchanged from Sprint 65; not touched this sprint) | Writing any other file; these writers have no path parameter at all — the destination is a Python constant, not runtime input |
| `home_assistant`, `windows`, `camera_ptz` tools | Remote API calls / allowlisted app launch only (unchanged from Sprint 65) | Any local filesystem write — none of these handlers open a file for writing anywhere in their `execute()` path |
| Tool registry (`ToolRegistry`) | Registration only from `bootstrap/adapters.py` / `builtin/__init__.py::register_all()` at process startup | Registration or mutation from any runtime tool call, LLM output, or conversation-derived input |
| `config/apps.json`, `lights.config.json`, `switches.config.json`, `scripts.config.json`, `persona.json` | Read-only at runtime (unchanged from Sprint 65) | Zero write-mode `open()` call sites reach these anywhere in `luno/` |

All paths above are real paths/constants from this codebase, not invented
examples.

## Phase 8 — Adversarial test matrix (A–U)

All 21 applicable cases (some collapse together where the underlying
mechanism is identical) implemented in
`tests/test_sprint66_tool_boundary_hardening.py`, every one operating
against either the real production roots (read-only comparison, no
writes) or a synthetic `tmp_path` fixture (`_make_synthetic_project()` /
`_make_synthetic_mimic_tree()`) with `SOURCE_ROOT`/`PROJECT_ROOT`/
`_collect_critical_paths` monkeypatched — never the real checkout for any
write attempt:

A (valid dir — accepted), B (source root itself — rejected), C (source
child dir — rejected), D (parent of project root — rejected), E (sibling
directory — accepted, since a sibling doesn't overlap any boundary), F
(`../` traversal in a destination filename — rejected by
`validate_download_path`), G (absolute-path escape — rejected), H
(Windows drive-letter variation — structural proof via
`_resolve_for_comparison`'s primitives), I (case variation — proof via
`normcase`), J (trailing separator — proven equivalent, no bypass), K
(symlink escape — three tests against a real symlinked synthetic tree,
all correctly blocked), L (junction/reparse-point escape — structural
proof; genuine platform limitation, see Known Limitations), M (empty
path), N (malformed path), O (nonexistent download directory — the
guard operates lexically and does not require existence, so a
not-yet-created but safe directory still validates correctly; an unsafe
one is still rejected regardless of existence), P (unknown tool — fails
closed with `error_type="unknown_tool"`), Q (unknown action — `validate()`
rejects before `execute()` runs), R (module-path-shaped tool name, e.g.
`"os.system"` — not in the registry, fails the same as any unknown
name), S (executable-path-shaped tool name, e.g. `"/bin/sh"` — same), T
(registry mutation attempt — proven via the absence of any handler-held
registry reference plus the AST call-site inventory), U (tool argument
attempting to redirect the output path — a real
`RealBrowserHandler.execute()` call with a `../`-laden `filename` is
exercised end-to-end and confirmed rejected).

Every unsafe case fails closed, performs zero filesystem writes outside
its own `tmp_path` fixture, executes no code, and never touches the real
production checkout.

## Phase 9 — Negative controls

The real production default (`config/browser_downloads`) is confirmed to
still pass `validate_download_directory()` (case A). The known-valid
tool/action dispatch path (`browser`/`download` with a safe filename) is
exercised end-to-end in the test suite and confirmed to still succeed.
Hardening added zero new rejections for any previously-legitimate
operation — every negative-control case in the new test file passes
alongside every adversarial case failing closed.

## Phase 10 — Critical file protection via the tool boundary

Protection is enforced entirely through `validate_download_directory()`
and the upgraded `validate_download_path()` — the only filesystem-write
surface reachable through a tool call at all (per Phase 7's matrix). No
generic global filesystem ACL and no manual OS permission change was
added, per the brief's own instruction to secure the surface, not bolt on
an unrelated mechanism. `*.py` files, `config/*.json`, `.env`, and the
tool registry/startup files are covered as CRITICAL_PATHS individual
entries or via the SOURCE_ROOT containment check; files entirely outside
what the browser download surface could ever reach (e.g. files on a
disk the process has no access to at all) are out of scope for this
guard by construction — this tool boundary can only protect what the
tool it's attached to could otherwise reach.

## Phase 11 — Explicit self-modification lock tests

`tests/test_sprint66_tool_boundary_hardening.py` proves, with dedicated
tests, that Luno still cannot: write an arbitrary `.py` file anywhere;
overwrite an existing source file; delete a source file; rename a source
file; execute generated Python; execute arbitrary shell (verified via an
AST inventory of every real `subprocess`/`os.system` call site, confirming
each is a fixed, non-LLM-controlled command — `desktop_control.py`'s
allowlisted `open_app`, `dashboard/collectors.py`'s `nvidia-smi` query,
`bootstrap/version.py`'s `git rev-parse --short HEAD`,
`main_runtime_demo.py`'s hardcoded `cls`/`clear`); dynamically install a
new tool or plugin; dynamically register a new tool from within a tool
call; perform a git write (push/commit/add/clone/checkout/reset/merge/
rebase — none found as a real call site, only docstring/comment prose
mentioning `pip install` as human-facing setup instructions, explicitly
excluded from the check). Every one of these capabilities was already
absent before this sprint (per Sprint 65's audit); these tests lock that
absence in with an executable regression guard rather than leaving it as
a one-time audit finding.

## Phase 12 — Persistent state safety

SHA-256 of all 15 `config/*.json` files and 11 critical source/config
files (`ARCHITECTURE_GUARD.md`, `luno/tool_manager/manager.py`,
`luno/tool_manager/registry.py`, `luno/desktop_control.py`,
`luno/browser/security.py`, `luno/browser/permissions.py`,
`luno/browser/config.py`, `luno/tool_manager/builtin/real_browser.py`,
`luno/config.py`, `main.py`, `main_runtime_demo.py`) was captured
immediately after this sprint's own deliberate code edits and again after
the full test/regression run. Byte-for-byte identical in every case —
zero drift, including zero drift in `security.py`/`real_browser.py`
themselves (confirming the test suite reads but never writes them).

## Phase 13 — Regression sequence

1. `tests/test_sprint66_tool_boundary_hardening.py` — 40 passed, 0 failed.
2. Combined targeted batch (Sprint 66's 40 + Sprint 65's 27 +
   `luno/tool_manager/tests/` + `tests/test_browser_wiring.py` +
   `tests/test_desktop_control.py`) — 198 passed, 0 failed.
3. Full repository sweep (`pytest tests/ -q --continue-on-collection-errors
   --ignore=tests/test_main_bargein.py --timeout=60`) — 3316 passed, 38
   failed, 3 skipped, 1 collection error, 749s. Every failing test name
   matches the file/test set Sprint 65's own baseline already classified
   as full-suite-only timing/environment-coupled flakiness (dashboard,
   emotion_engine's stale-decay test, llm_dashboard,
   llm_tts_streaming_production, mic_device_index — missing
   `list_microphones.py` dependency, production_launcher — network
   health checks, real_adapters — missing `speech_recognition`/
   `sounddevice`, runtime_demo's episodic-memory test, state_isolation,
   streaming_e2e, streaming_speech_integration, tts_chunk_pipelining,
   tts_e2e_pipeline, verification_dashboard, vision_ask_vision,
   vision_sprint8, voice_pipeline_latency); the collection error is
   `test_root_main_bargein.py`'s pre-existing, already-documented
   `legacy_main.py`-absent INFRASTRUCTURE issue (unrelated to this
   sprint — this sprint's sweep command omitted the second
   `--ignore` flag Sprint 65's baseline command used for that file,
   which is why it surfaced as a collection ERROR here instead of being
   silently skipped; the underlying cause is identical and already
   documented). A representative sample of 33 of the 38 failing tests
   (`test_vision_ask_vision.py` in full, plus one test each from
   `test_dashboard.py`, `test_emotion_engine.py`, and
   `test_tts_chunk_pipelining.py`) was re-run in isolation and passed
   33/33 — confirming the full-suite-only timing class, not a genuine
   regression. Zero tests newly failing that weren't already in this
   documented class.

## Phase 14 — Performance

`validate_download_directory()` and the tool registry `.get()` lookup are
both measured, in dedicated timed tests, at well under the 5ms/operation
target. Neither makes a network call, an LLM call, or unnecessary
blocking I/O — both are pure path-string computation plus (for the
directory guard) a handful of `os.path` syscalls.

## Security guarantees now in place

- `download_dir` is validated both at handler-construction time and on
  every download call — a misconfiguration can no longer silently
  produce an unsafe download destination; it fails closed instead.
- Symlink-based escape from an otherwise-safe `download_dir` is closed at
  the per-file level (`validate_download_path()`'s upgrade), in addition
  to the directory-level guard already resolving symlinks correctly.
- The tool registry's already-existing safety properties (closed
  namespace, no runtime mutation path, no dynamic import/eval) now have
  an executable regression guard (Sprint65-001's finding addressed).
- Self-modification, arbitrary file write, and arbitrary code execution
  all remain provably absent, now with dedicated lock-down tests rather
  than resting solely on a one-time audit.

## Known limitations

This sandbox is Linux — true Windows case-insensitive filesystem
behavior and junction/reparse-point creation could not be executed and
observed directly, only proven structurally via the documented behavior
of `os.path.realpath()`/`os.path.normcase()` on Windows since Python
3.8. The "`download_dir` may nest under `PROJECT_ROOT`" interpretation
(Phase 2) is a documented judgment call reconciling two parts of the
brief, not a literal transcription of its wording — reviewers should
read that reasoning and confirm it matches intent. No automated CI gate
runs this sprint's tests on every commit yet (same limitation Sprint 65
noted for Finding SPRINT65-001) — the tests exist and pass, but nothing
outside a manual `pytest` invocation currently enforces them.

## Remaining UNKNOWN

Chain G — whether the actual deployed Home Assistant instance/network
could be configured in a way that reaches back into this project's
filesystem — remains UNKNOWN, unchanged from Sprint 65's own conclusion.
This is unknowable from source code alone and out of scope for a
source-code-level hardening sprint.
