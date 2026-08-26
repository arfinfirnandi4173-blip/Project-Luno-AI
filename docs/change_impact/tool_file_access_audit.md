# Sprint 65 — Luno Tool & File Access Audit

**Type:** Audit only. **Zero production code changes.** Every claim below
is backed by a source-code citation and/or an executable test in
`tests/test_sprint65_tool_file_access_audit.py`. Per the brief's own
rule, source code is authority over documentation wherever the two
disagree, and anything not provable from source/test is marked UNKNOWN
rather than assumed safe.

**Primary question:** *Is there a path by which Luno, directly or through
a combination of tools, can modify its own source code/configuration/
project files?*

**Short answer:** No such path is provable from this codebase today.
Every write-capable surface this audit found is either (a) contained to a
JSON data/memory store, never a `.py`/config source file, or (b)
contained to a designated download directory via a path-containment
check whose safety currently rests on that directory never overlapping
the source tree (verified true today, but not a code-level invariant —
see Finding SPRINT65-002). No exec()/eval()/shell=True/dynamic
plugin-loading mechanism exists anywhere in production code.

---

## Phase 1 — Tool inventory & capability matrix

Traced every registered tool handler from `luno/bootstrap/adapters.py`
(the real-handler wiring `main.py` actually calls) and
`luno/tool_manager/builtin/__init__.py::register_all()` (the mock-only
convenience registrant), down to each handler's `execute()` implementation.

| TOOL | ENTRY POINT | CALLER | READ | WRITE | DELETE | RENAME | EXECUTE | NETWORK | ARBITRARY PATH | RESTRICTION | PROOF |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `home_assistant` | `RealHomeAssistantHandler.execute()` | `ToolManager` via `Planner`/`main_runtime_demo.py` task dispatch | remote HA state only | remote HA service calls only (no local filesystem) | no | no | no (remote service calls, not local processes) | yes (HA WebSocket API) | no — `entity_id`/domain/service resolved only through `devices.py`'s registries (`lights.config.json`/`switches.config.json`/`scripts.config.json`) and a small `_SUPPORTED_ACTIONS` set | allowlist-only device/script resolution; `action` validated against 7 fixed values before `execute()` runs | `luno/tool_manager/builtin/real_home_assistant.py`; `luno/ha_client.py:119` (`call_service`); `devices.py` registries |
| `windows` | `RealWindowsHandler.execute()` | same | no | no | no | no | **yes** — `open_app`/`launch_app` only | no | no — `name` is a dict-lookup key into `config/apps.json`'s allowlist, never a raw path | allowlist-only (`config/apps.json`); every other simulated action (`close_app`, `shutdown`, `restart`, etc.) fails honestly as "not implemented" | `luno/tool_manager/builtin/real_windows.py`; `luno/desktop_control.py::open_app()` |
| `browser` | `RealBrowserHandler.execute()` | same | yes (page text/title/URL/screenshot) | **yes** — `download` action only | no | no | no (browser automation, not OS processes) | yes (whatever domain the page navigates to, subject to allowlist) | download destination is contained via `validate_download_path()`; navigation is contained via `is_domain_allowed()` (opt-in allowlist, empty = unrestricted) | `PermissionManager` risk classification (READ_ONLY/LOW_RISK/SENSITIVE/HIGH_RISK) gates every action; `security.redact_secrets()` scrubs output | `luno/tool_manager/builtin/real_browser.py`; `luno/browser/security.py`; `luno/browser/permissions.py` |
| `camera_ptz` | `RealCameraPTZHandler.execute()` | same | camera state/snapshot | no | no | no | no | yes (Tapo camera API) | no | fixed action set for a single configured camera client | `luno/tool_manager/builtin/real_camera_ptz.py` |
| `vision`, `spotify`, `unity`, `llm_mode`, `dummy` | mock handlers only (`register_all()`) | test/demo wiring only — **not** wired into production `bootstrap/adapters.py`'s real registration path | mock/no-op | mock/no-op | no | no | no | no | no | no real backing implementation exists for these in production | `luno/tool_manager/builtin/__init__.py`; absence confirmed by grepping `bootstrap/adapters.py` for each name |
| *(none — no generic execution tool exists)* | — | — | — | — | — | — | — | — | — | **no tool named `python`/`shell`/`exec`/`bash`/`cmd`/`powershell`/`run_command`/`subprocess`/`code` is ever registered anywhere** | `test_A_builtin_register_all_only_registers_the_known_mock_set`, `test_A_bootstrap_only_wires_the_known_closed_set_of_real_handlers` |

`ToolManager._run_single_attempt()` (`luno/tool_manager/manager.py:196-231`)
looks `call.tool` up in a `ToolRegistry` (a plain `Dict[str, ToolHandler]`)
and returns a clean `ToolResult.fail(..., error_type="unknown_tool")` on a
miss — it never falls back to executing arbitrary text as code. Every
handler's `validate()` additionally rejects any `action` outside that
handler's own fixed `supported_actions()` list before `execute()` is ever
called. **The tool name and action namespaces are both closed, enumerated
sets fixed at registration time — not something conversation/LLM text can
expand at runtime.**

## Phase 2 — Filesystem access audit

Every `open()`/`Path`/`os.rename`/`os.replace`/`shutil.*`/`tempfile.*`
call site in `luno/` was enumerated (see
`test_D_read_only_config_files_have_zero_write_mode_open_calls_anywhere`,
`test_F_zero_shell_equals_true_anywhere_in_production_code`, and the
Sprint 63/64 writer inventories this sprint reuses). No `zipfile`,
`tarfile`, `pickle`, or `yaml`-write usage exists anywhere in this
codebase.

### A. READ ONLY
`PERSONA_FILE`, `APPS_CONFIG_FILE`, `LIGHTS_CONFIG_FILE`,
`SWITCHES_CONFIG_FILE`, `SCRIPTS_CONFIG_FILE` — loaded once at import
time via a bare `open(path, "r", ...)`, never opened in a write mode
anywhere in production code (`test_D_read_only_config_files_have_zero_write_mode_open_calls_anywhere`).
Editing these is an out-of-band, human, filesystem-level action only —
no runtime code path can change them.

### B. CONTROLLED WRITE
- `LONG_TERM_MEMORY_FILE`, `SESSION_SUMMARIES_FILE`, `VERIFIED_FACTS_FILE`,
  `HABIT_MEMORY_FILE`, `EPISODIC_MEMORY_FILE`, `RELATIONSHIP_STATE_FILE`,
  `RESPONSE_DEPTH_PREFERENCE_FILE`, `REMINDERS_FILE` — each written only
  through its own dedicated persistence function (`luno.memory._save()`
  or a `luno.persistence.atomic_write_json()` caller bound to that
  store's own path constant; see Sprint 64's own writer inventory,
  reused here). Every path is a hardcoded module-level constant in
  `luno/config.py`, assembled from `DATA_DIR` (an env-controlled
  deploy-time value) and a fixed literal filename — never string-built
  from conversation/LLM text (`test_D_every_data_dir_path_constant_is_a_hardcoded_module_level_constant`,
  `test_D_no_config_dot_json_path_is_ever_built_from_conversation_or_llm_text`).
  **These are all JSON data/memory stores, never `.py` source or executable
  config.** Content written to them is structured (dict/list shapes going
  through `json.dump`), not raw LLM text verbatim.
- Browser `download` action — writes exactly one file, whose *filename*
  component is LLM/tool-argument-controlled (`params.get("filename")`),
  contained inside `BrowserConfig.download_dir` via
  `validate_download_path()`. See Finding SPRINT65-002 below for the
  precise boundary and its dependency on configuration.
- `AUDIO_SERVE_DIR` (`tempfile.gettempdir()/luno_audio_cast`) and
  ad-hoc `tempfile.NamedTemporaryFile()` WAV files in `luno/main.py` —
  audio pipeline scratch files, always created and deleted by the same
  code, never a source-adjacent path.

### C. ARBITRARY WRITE
**None found.** No code path accepts an unconstrained destination path
from conversation/LLM text and writes to it.

### D. DELETE
`os.remove()` call sites are all scoped to files the same code created
or manages: temp-file cleanup after a failed/successful atomic write
(`luno/memory.py`, `luno/persistence.py`), backup-retention pruning
(same two modules — never below 1 backup), TTS/whisper temp-audio
cleanup (`luno/main.py`), and log-file/test-artifact rotation
(`luno/dashboard/event_log_writer.py`, `luno/test_capture.py`). None
accept an LLM/conversation-supplied path.

### E. RENAME/MOVE
`os.replace()` (never `os.rename()`, deliberately — atomic overwrite on
both POSIX and Windows) appears only inside the persistence hardening
layer's own temp-file → final-path swap, always the same fixed pair of
paths (`{path}.tmp` → `path`) for a hardcoded store path. No `shutil.move`
usage exists in production code.

### F. EXECUTABLE/PROCESS
`subprocess.Popen`/`os.startfile` in `luno/desktop_control.py::open_app()`
(allowlist-only, see Phase 1 table); `subprocess.run` in
`luno/dashboard/collectors.py` (`nvidia-smi`, fixed argv, best-effort
GPU stats) and `luno/bootstrap/version.py` (`git rev-parse`, fixed argv,
version banner); one `os.system("cls"/"clear")` in
`main_runtime_demo.py` (hardcoded terminal-clear, chosen by `os.name`,
never conversation input). **Zero `shell=True` anywhere**
(`test_F_zero_shell_equals_true_anywhere_in_production_code`). Every
argv is a Python list, never a single interpolated string.

### G. UNKNOWN
None — every filesystem-touching code path in `luno/` was traceable to a
concrete classification above.

### Path origin, for every write-capable path found
| Path | Origin |
|---|---|
| All `*_FILE` constants (memory/state stores) | hardcoded constant (`DATA_DIR` + literal filename) |
| `download` action's filename | tool argument (LLM/caller-supplied), contained by `validate_download_path()` |
| `AUDIO_SERVE_DIR` / temp WAV paths | hardcoded (`tempfile` module functions) |
| `apps.json`'s registered app `path`/`args` | config file (human-edited, out-of-band) |
| HA `entity_id`/domain/service | resolved through `devices.py` registries (config files), not raw LLM text |

**Can LLM/Luno influence the path destination?** Only in one place: the
browser `download` action's *filename* (not directory). Everywhere else,
the destination path is either fully hardcoded or resolved through a
registry/allowlist keyed by name, never built directly from free text.

## Phase 3 — Source code access

1. **Can Luno read source code?** Yes, trivially — this session's own
   tools (Read/Grep/Bash) read `.py`/`.json`/`.md`/etc. freely, and
   nothing in the runtime *application* itself is prevented from reading
   its own files either (no sandboxing prevents `open("luno/memory.py")`
   from Python code, though no production code path actually does this
   at runtime — there's no "read my own source" feature).
2. **Can Luno generate source code as text?** Yes — an LLM can produce
   arbitrary text, including text shaped like Python source, in a chat
   response. This is a property of the LLM, not of this codebase, and by
   itself has no effect on the filesystem.
3. **Can Luno save generated source code to the filesystem?** Not through
   any production tool. No registered tool accepts "write this text to
   this path" as an action (the only file-writing tool action is
   `browser`'s `download`, which fetches bytes from a URL via the
   browser provider — it does not write LLM-authored text to disk).
4. **Can Luno replace existing source code?** Not provable from any code
   path in this checkout — see Phase 4/9 below.
5. **Can Luno execute source code it produced?** Not provable — no
   exec()/eval() call site exists in production code
   (`test_E_no_exec_or_eval_call_sites_exist_in_production_code`), and
   the only dynamic module-loading mechanism
   (`importlib.util.spec_from_file_location`) targets two fixed,
   hardcoded filenames (`legacy_main.py`, `main_runtime_demo.py`), never
   a name built from conversation/LLM text
   (`test_E_every_dynamic_module_load_targets_a_hardcoded_fixed_filename`).

**Capability 1 (read) does NOT imply capability 4 (replace)** — this
audit found no code path connecting them. Reading source and modifying
source are structurally unrelated capabilities in this codebase; the
former exists (in this investigative session, and in principle for any
Python process), the latter is not evidenced anywhere.

## Phase 4 — Self-modification path analysis

| Chain | Status | Evidence |
|---|---|---|
| A. LLM → tool call → filesystem write → source file | **BLOCKED** | No registered tool's `execute()` writes to a `.py`/`.json` config path from an LLM-supplied destination. The one LLM-influenceable write (`download`'s filename) is contained inside `BrowserConfig.download_dir`, a dedicated leaf directory that never overlaps `luno/`'s source tree in the current configuration (`test_C_real_browser_config_default_download_dir_never_overlaps_the_luno_source_tree`) — see Finding SPRINT65-002 for the config-dependent caveat. |
| B. LLM → Python execution → open() → source file | **IMPOSSIBLE** (as currently structured) | There is no "run this Python" tool or exec()/eval() call site anywhere in production code for the LLM to reach in the first place (`test_E_no_exec_or_eval_call_sites_exist_in_production_code`). This chain has no first link. |
| C. LLM → shell execution → command → source file | **IMPOSSIBLE** (as currently structured) | No tool exposes shell/command execution to the LLM; the only OS-process-spawning tool (`windows`/`open_app`) is allowlist-only and only ever launches a registered app, never a shell (`test_B_open_app_refuses_any_name_not_in_the_allowlist`, `test_B_open_app_never_falls_back_to_an_unregistered_arbitrary_path`). |
| D. LLM → generated file → import/reload → runtime behavior | **BLOCKED** | No directory-scan-and-import mechanism (`pkgutil`/`iter_modules`/`runpy`/a loop over `os.listdir()` results feeding `importlib.import_module`) exists anywhere (`test_E_no_directory_scan_and_import_plugin_loading_pattern_exists`). Nothing "notices" a new file and imports it. |
| E. LLM → config modification → restart/reload → altered capabilities | **BLOCKED for capability-granting configs; PROVEN for behavior-shaping state** | `apps.json`/`lights.config.json`/`switches.config.json`/`scripts.config.json`/`persona.json` (the files that could grant new *tool* capabilities or change what commands do) are read-only at runtime — zero write-mode `open()` call sites anywhere (`test_D_read_only_config_files_have_zero_write_mode_open_calls_anywhere`). Separately, memory/preference stores (`RESPONSE_DEPTH_PREFERENCE_FILE`, `RELATIONSHIP_STATE_FILE`, etc.) genuinely ARE written by designed conversational features and do shape future *behavior* (tone, remembered facts, response depth) — this is an intended product feature, not source-code self-modification, and grants no new tool/capability. |
| F. LLM → tool/plugin configuration → new capability | **IMPOSSIBLE** (as currently structured) | Tool registration (`registry.register(name, handler)`) happens only at startup, from fixed Python code in `bootstrap/adapters.py` — there is no runtime API, tool, or config file that adds a new registry entry. |
| G. LLM → external API → service with filesystem access → project modification | **UNKNOWN** | The Home Assistant WebSocket API and the browser's navigable web are both external services this codebase talks to. Neither is known to have filesystem access to *this* project's checkout, but this audit cannot prove a negative about arbitrary external service configuration (e.g. if HA were somehow configured with a shell-command add-on pointed at this checkout, or DNS/network routing changed what "browser" traffic actually reaches) — that configuration lives entirely outside this codebase and is unverifiable from here. Marked UNKNOWN per the brief's own rule rather than assumed safe. |

## Phase 5 — Indirect execution audit

- **Python:** `importlib.util` used twice, both hardcoded targets (see
  Phase 3/4). No `runpy`, no bare `exec()`/`eval()` in production code.
  No Jupyter/IPython execution anywhere.
- **OS:** no PATH-based arbitrary-executable invocation (every
  `subprocess`/`os.system`/`os.startfile` call site uses either a fixed
  literal or an allowlist-resolved path — see Phase 2.F). No Docker, SSH,
  PowerShell, or cmd.exe invocation found anywhere in this codebase
  (`grep` for `docker`, `ssh`, `powershell`, `cmd.exe` — zero
  non-comment/non-doc hits in `luno/`).
- **External:** Home Assistant service calls are structured JSON-RPC
  messages over WebSocket (`domain`, `service`, `entity_id`, `data` as
  distinct fields — never a raw string built by concatenation), with
  `domain`/`service` always drawn from a small hardcoded set and
  `entity_id` always resolved through a config-file-backed registry —
  see `luno/ha_client.py:119` and Phase 1's table. No webhook receiver,
  automation-trigger listener, or plugin system that could receive
  external commands was found in this codebase.
- **Browser `computer_use.py`:** click/type/scroll/navigate actions are
  dispatched only to a `BrowserProvider` (Playwright-backed) — bounded to
  interacting with a web page, not the OS. Every action passes through
  `PermissionManager.evaluate()` first, which can deny or require
  confirmation.

**No capability found anywhere in this codebase can theoretically run
arbitrary code**, as distinct from a fixed, enumerated set of
domain-specific actions.

## Phase 6 — LLM control boundary

| LLM CONTROLLED INPUT | VALIDATION? | NORMALIZATION? | AUTHORIZATION? | SINK | EFFECT |
|---|---|---|---|---|---|
| Tool name | yes — `ToolRegistry.get()` returns `None` on any name outside the fixed registered set | none needed (exact string match) | n/a (unknown tool fails closed) | `ToolManager._run_single_attempt()` | fails with `error_type="unknown_tool"`, nothing executes |
| Tool action | yes — `handler.validate()` checks `supported_actions()` | none | n/a | same handler's `execute()` | fails with `error_type="unknown_action"`/`validation_error` |
| App name (`windows`/`open_app`) | yes — dict-lookup against `config/apps.json` | `.strip().lower()` | allowlist only | `subprocess.Popen`/`os.startfile` with the CONFIG-sourced path, never the LLM-typed name itself | launches only a pre-registered app, or fails with a clean "not registered" message |
| Browser navigation URL | yes — `is_domain_allowed()` (opt-in allowlist; empty = unrestricted) | hostname extraction via `urlparse` | domain allowlist, when configured | `BrowserProvider.open_url()` (Playwright) | navigates a sandboxed browser tab; no local filesystem/process effect |
| Browser download filename | yes — `validate_download_path()` containment check | `os.path.abspath`/`os.path.commonpath` | none beyond path containment (no confirmation required — LOW_RISK) | filesystem write inside `download_dir` | writes exactly one file, whose *name* the LLM chose, inside the configured download directory only |
| HA entity/script target | yes — resolved through `devices.py` registries | fuzzy/slug matching against registered names only | allowlist only (registered devices/scripts) | `ha_client.call_service()` (remote WebSocket) | acts on a registered smart-home device/script only |
| Conversational text → `ParsedStep.tool`/`.action` | yes — `IntentParser`'s fixed regex grammar; anything unmatched falls to the `"unknown"` sentinel, itself filtered out before task dispatch (`tool_call.tool != "unknown"` guards in `main_runtime_demo.py`) | regex-based clause splitting/slugification | n/a | `Planner`/`TaskExecutor` → `ToolManager` | only ever produces a `ToolCall` shaped from the closed tool/action grammar above — never arbitrary text passed straight to a sink |

**No LLM-controlled input in this codebase reaches a sink capable of
arbitrary filesystem write, arbitrary command execution, or dynamic code
execution.** Every sink that accepts LLM-influenced input is either a
closed enumeration (tool/action names), a registry/allowlist lookup
(app names, HA entities), or a path-containment check (download
filename).

## Phase 7 — Self-modification experiments (synthetic fixtures only)

All experiments below ran against a synthetic `tmp_path` project
(`protected.py`, `marker.txt`, a `downloads/` subdirectory) or a
monkeypatched `desktop_control.APPS` dict — **never** the real checkout,
`E:\Luno Evo`, production config, or real memory files. See
`tests/test_sprint65_tool_file_access_audit.py`'s `C`/`B` sections for
the executable, repeatable form of each experiment.

| Experiment | Result |
|---|---|
| Can the download path escape via `../`? | **No** — rejected by `validate_download_path()`. |
| Can an absolute path outside `download_dir` be used? | **No** — rejected (`os.path.commonpath` containment check). |
| Can a nested relative path create a subdirectory inside `download_dir`? | **Yes** — this is within the intended containment boundary, not a traversal. |
| What if `download_dir` were misconfigured to equal the project root? | **The path-containment check alone would then allow overwriting an existing file with an LLM-chosen name** — the code has no independent check for "am I inside a source directory," only "am I inside whatever `download_dir` is." This is Finding SPRINT65-002, confirmed as currently NOT the real configuration (`test_C_real_browser_config_default_download_dir_never_overlaps_the_luno_source_tree`). |
| Does `open_app()` accept an unregistered raw path (e.g. `/bin/sh`)? | **No** — fails cleanly, "not registered." |
| Does `open_app()`'s name lookup have a case/whitespace bypass? | **No** — normalizes via `.strip().lower()` only; a shell-metacharacter-suffixed name (`"notepad; rm -rf /"`) does not fuzzy-match the allowlist. |
| Is `open_app()`'s subprocess call ever shell-interpreted? | **No** — always a list argv to `subprocess.Popen`, never `shell=True`. |
| Does `IntentParser.parse()` ever emit a tool name derived from injected shell-metacharacter text? | **No** — fuzzed with six injection-shaped strings (`; rm -rf /`, `$(...)`, `` ` ` ``, path traversal, `file://`); every resulting `ParsedStep.tool` stayed within the known closed set. |

## Phase 8 — Critical file classification

| Classification | Files | READ | WRITE | DELETE | EXECUTE |
|---|---|---|---|---|---|
| **CRITICAL — source code** | everything under `luno/`, `main.py`, `main_runtime_demo.py`, `probe_memory_pipeline.py` | yes (this session's own tools; no runtime "read my source" feature) | **no runtime code path found** | no | yes, by the Python interpreter at process start (not something the LLM triggers mid-session) |
| **CRITICAL — config** | `config/*.json` (apps/lights/switches/scripts/env-triggers) | yes, at startup | **no** for apps/lights/switches/scripts (read-only, proven); memory/state files ARE written by their own dedicated store logic (see Phase 2.B) | no | n/a |
| **CRITICAL — memory stores** | `long_term_memory.json`, `episodic_memory.json`, `session_summaries.json`, etc. | yes | yes, via each store's own dedicated writer only | no (only backup pruning of files the writer itself created) | n/a |
| **CRITICAL — credentials** | `.env` (`HA_TOKEN`, `OPENAI_API_KEY`, etc.) | yes, at startup via `python-dotenv` | no | no | n/a — never written by runtime code; `redact_secrets()` scrubs anything secret-shaped before it could reach an LLM prompt |
| **CRITICAL — tool/plugin registry** | `luno/tool_manager/registry.py`, `luno/bootstrap/adapters.py` | yes (source) | no runtime write path | no | registration happens only once, at process startup, from fixed code |
| **CRITICAL — startup/launcher** | `main.py`, `luno/bootstrap/*` | yes (source) | no runtime write path | no | yes, at process start only |
| **IMPORTANT — tests** | `tests/*.py` | yes | no production runtime writes here (this audit's own new file only touches synthetic fixtures) | no | yes, via `pytest`, a developer/CI action, not an LLM-triggered runtime path |
| **IMPORTANT — docs** | `docs/*`, `ARCHITECTURE_GUARD.md` | yes | no runtime write path (this sprint's own docs were written by this development session, not by Luno's own runtime) | no | n/a |
| **IMPORTANT — generated artifacts** | none identified — no code path generates persistent artifact files at runtime beyond the memory/state stores already covered | — | — | — | — |
| **NON-CRITICAL — cache** | `__pycache__/`, `.pytest_cache/` | n/a | yes, by the Python interpreter/pytest itself (standard bytecode caching) | yes, standard cache eviction | n/a |
| **NON-CRITICAL — temp/logs** | `logs/`, temp WAV files, `AUDIO_SERVE_DIR` | yes | yes, by the logging/audio pipeline itself | yes, by that same pipeline's own cleanup | n/a |

## Phase 9 — The real answer

**A. Can Luno currently modify its own source code?** No provable path
found. Every write-capable surface in this codebase either targets a
JSON data/memory store (never `.py`/config source) or is contained
within a designated, non-source directory by a path-containment check.

**B. If yes:** N/A — no such path was found.

**C. What is the boundary, and is it enforced by code or convention?**
The boundary is enforced by code in the places that matter most: the
tool/action namespaces are closed Python data structures populated once
at startup (not something any runtime input can extend), there is no
exec()/eval()/shell=True/dynamic-plugin-loading mechanism to escape
through, and the one LLM-influenceable filesystem write
(`download`'s filename) is code-enforced to stay inside `download_dir`.
The one place the boundary rests on **configuration rather than code**
is that `download_dir` itself is not validated to be disjoint from the
source tree — see Finding SPRINT65-002. Every read-only config file
(`apps.json`, `lights.config.json`, etc.) is enforced read-only by the
simple fact that no write-mode `open()` call site exists against those
constants anywhere — also a code property, not just convention, though
it would only take one new call site in a future sprint to change that
(there's no automated guard preventing someone from adding one — see
Known Limitations).

**D. Can Luno modify configuration that then changes its own behavior?**
Yes, for the memory/preference stores specifically (`RESPONSE_DEPTH_PREFERENCE_FILE`,
`RELATIONSHIP_STATE_FILE`, etc.) — this is a designed, intended
adaptive-assistant feature (remembering facts, adjusting tone), not
source-code self-modification, and it grants no new tool capability. No,
for the files that would grant new *capabilities* (`apps.json` and the
HA device/script registries) — those are read-only at runtime.

**E. Can Luno create new executable Python/source and then run it?** Not
provable — no tool writes arbitrary LLM-authored text to a `.py` file,
and even if such a file existed on disk, no mechanism in this codebase
would notice and import/execute it (see Phase 4, chains B and D).

**F. Is there an indirect route via Home Assistant, HTTP, plugin,
subprocess, Docker, or service manager?** Every one of these was
searched. Subprocess use is allowlist-bound (`open_app`) or fixed-argv
diagnostic (`nvidia-smi`, `git`). No Docker/SSH/service-manager
invocation exists in this codebase. HA calls are structured, allowlist-
resolved service calls, not filesystem operations. No plugin/webhook
receiver exists. The only genuinely unresolved item is Chain G (external
service configuration outside this codebase's own control) — marked
UNKNOWN, not ruled out, because it is outside what this checkout can
prove.

## Phase 10 — Security findings

**FINDING SPRINT65-001**
- **Severity:** INFO
- **Title:** Tool/action namespaces are closed by convention-enforced-as-code, with no automated regression guard against a future addition
- **Source:** `luno/tool_manager/registry.py`, `luno/bootstrap/adapters.py`
- **Attack/capability path:** none today — this finding documents the CURRENT safety property (closed registry) and notes that nothing would automatically flag a future sprint that registered a new tool with, say, arbitrary shell access.
- **Evidence:** `test_A_bootstrap_only_wires_the_known_closed_set_of_real_handlers`, `test_A_builtin_register_all_only_registers_the_known_mock_set` — both pass today and would fail if a forbidden generic-execution tool name were ever registered, but nothing outside this audit's own tests enforces that.
- **Current impact:** none.
- **Exploitability:** not applicable (not a vulnerability, a structural observation).
- **Confidence:** HIGH (directly evidenced).
- **Recommendation:** *(not to be implemented this sprint, per the brief)* — consider keeping `test_A_*` in this file as a permanent regression guard in future sprints' own test runs.

**FINDING SPRINT65-002**
- **Severity:** LOW
- **Title:** Browser download path containment is correct, but its safety boundary is a configuration invariant, not a code-level guarantee
- **Source:** `luno/browser/security.py::validate_download_path()`, `luno/browser/config.py::BrowserConfig.download_dir`
- **Attack/capability path:** IF `BROWSER_DOWNLOAD_DIR` were ever set (or its default changed) to overlap the `luno/` source tree or project root, THEN a `download` action — LOW_RISK, auto-allowed without confirmation by default — with an LLM/tool-argument-controlled `filename` could resolve onto an existing source file's path and overwrite it with fetched content.
- **Evidence:** `test_C_download_path_containment_depends_entirely_on_download_dir_configuration` reproduces this deterministically against a synthetic fixture; `test_C_real_browser_config_default_download_dir_never_overlaps_the_luno_source_tree` confirms the CURRENT real default (`<DATA_DIR>/browser_downloads`) does not hit this.
- **Current impact:** none under the current, verified default configuration.
- **Exploitability:** requires a configuration change (`BROWSER_DOWNLOAD_DIR` env var) that is not itself reachable from any LLM/conversation-controlled input in this codebase — it is a deploy-time environment variable.
- **Confidence:** HIGH (directly evidenced, both the risk and its current non-exposure).
- **Recommendation:** *(not to be implemented this sprint)* — a future hardening sprint could add a startup-time assertion that `download_dir` is disjoint from the project's own source root, independent of correct env configuration.

**FINDING SPRINT65-003**
- **Severity:** INFO
- **Title:** External-service-mediated filesystem access (Chain G) cannot be proven or disproven from this codebase alone
- **Source:** N/A — this is a boundary-of-knowledge finding, not a code finding.
- **Attack/capability path:** UNKNOWN — would require external Home Assistant/network configuration this checkout has no visibility into.
- **Evidence:** absence of any in-repo webhook/automation-trigger/plugin receiver; absence of any code path that would let such external configuration reach back into this checkout even if it existed.
- **Current impact:** UNKNOWN.
- **Exploitability:** UNKNOWN.
- **Confidence:** LOW (genuinely unresolvable from this vantage point — reported as UNKNOWN per the brief's own rule, not assumed safe).
- **Recommendation:** *(not to be implemented this sprint)* — out of scope for a codebase-only audit; would require inspecting the actual deployed HA instance/network, which this sandbox cannot do.

No CRITICAL or HIGH severity findings were identified. No STOP CONDITION
from Phase 15 was triggered (see below).

## Phase 15 — Stop conditions evaluated

1. Arbitrary filesystem write to a production path with unclear
   authorization boundary — **not triggered**: the one LLM-influenceable
   write (download filename) has a clear, code-enforced containment
   boundary; its dependency on configuration is documented as
   SPRINT65-002 (LOW), not left unclear.
2. Arbitrary command execution influenceable by the LLM — **not
   triggered**: no such execution path was found.
3. A self-modification path that genuinely reaches source code — **not
   triggered**: no such path was found.
4. Reproduction requiring touching the production checkout — **not
   triggered**: every Phase 7 experiment used `tmp_path`/monkeypatched
   fixtures only.
5. Persistent state changed unintentionally — **not triggered**: see
   Phase 14 verification below (zero drift).
6. Credential exposure indication — **not triggered**: no runtime code
   path writes or exposes `.env` values; `redact_secrets()` exists
   specifically to keep secret-shaped text out of LLM-visible output.
7. A capability that cannot be classified with adequate confidence —
   **partially triggered, documented as UNKNOWN rather than guessed**:
   Chain G (external-service-mediated access) and Finding SPRINT65-003
   are reported as genuinely unresolvable from this codebase, per the
   brief's own instruction to mark UNKNOWN rather than assume safety.

No full STOP was required — the one place evidence ran out (Chain G) was
handled by marking it UNKNOWN, exactly as the brief instructs, rather than
halting the entire audit.

## Known limitations

- This audit is scoped to what is provable from source code and
  executable tests in this checkout. It cannot inspect the actual
  deployed Home Assistant instance, network configuration, or any
  external service's own filesystem access (Chain G, Finding
  SPRINT65-003).
- No automated CI/regression gate currently prevents a *future* sprint
  from registering a new tool with broader capabilities (Finding
  SPRINT65-001) or from adding a write-mode `open()` call site against a
  currently-read-only config constant — this audit's own tests would
  catch such a change if re-run, but nothing runs them automatically on
  every commit in this environment.
- The MIT-license-search-style approach of scanning installed packages
  was not repeated here; this audit is about Luno's own code paths, not
  third-party package behavior.
- This audit did not exercise the full running application end-to-end
  (no live console/LLM loop was started) — all findings are static/
  structural, plus targeted synthetic-fixture reproductions. A
  determined dynamic-analysis pass (actually running the assistant with
  adversarial prompts against a live tool-enabled session) was out of
  scope for an audit-only sprint and is listed as a natural next step.
