# Sprint 67 — Mutation Audit Trail & Forensic Observability

**Type:** Observability/forensics only. Adds no new capability to Luno -
no shell execution, no arbitrary Python execution, no arbitrary
filesystem write, no source-code modification, no git write, no plugin
installation, no dynamic tool registration. Everything below either
watches a mutation that was ALREADY going to happen, or proves an
absence.

**Prerequisite evidence read first (Phase 0):** `docs/change_impact/
long_term_memory_recovery.md` (Sprint 63), `docs/change_impact/
long_term_memory_corruption_forensics.md` (Sprint 64), `docs/change_impact/
tool_file_access_audit.md` (Sprint 65), `docs/change_impact/
tool_boundary_hardening.md` (Sprint 66), `ARCHITECTURE_GUARD.md` §§64-67,
`luno/memory.py`, `luno/persistence.py`, `luno/browser/security.py`,
`luno/tool_manager/`, `luno/dashboard/event_log_writer.py`.

---

## Phase 1 — Mutation surfaces (actual writers traced, not assumed)

| Surface | Writer | Caller | Path source | LLM-controllable | Atomic | Backup | Pre-Sprint-67 logging |
|---|---|---|---|---|---|---|---|
| A. `config/long_term_memory.json` | `luno.memory._atomic_write_json()` (private, own implementation) | `luno.memory._save()` | `luno.config.LONG_TERM_MEMORY_FILE` (env/config, fixed) | No | Yes (temp+fsync+`os.replace()`) | Yes (`_backup_current_memory_file()`) | print() only |
| B1. `config/session_summaries.json` | `luno.persistence.atomic_write_json()` | `luno.memory._save_session_summaries()` | `luno.config.SESSION_SUMMARIES_FILE` | No | Yes | Yes | print() only |
| B2. `config/verified_facts.json` | `luno.persistence.atomic_write_json()` | `luno.memory_guard.VerifiedFactsStore.save()` | `luno.config.VERIFIED_FACTS_FILE` | No | Yes | Yes | print() only |
| B3. `config/habit_memory.json` | `luno.persistence.atomic_write_json()` | `luno.proactive.habit_memory.HabitMemory.save()` | `luno.config.HABIT_MEMORY_FILE` | No | Yes | Yes | print() only |
| B4. `config/episodic_memory.json` | `luno.persistence.atomic_write_json()` | `luno.episodic_memory.EpisodicMemoryStore.save()` | `luno.config.EPISODIC_MEMORY_FILE` | No | Yes | Yes | print() only |
| B5. `config/relationship_state.json` | `luno.persistence.atomic_write_json()` | `luno.relationship_engine.RelationshipStore.save()` | `luno.config.RELATIONSHIP_STATE_FILE` | No | Yes | Yes | print() only |
| B6. `config/response_depth_preference.json` | `luno.persistence.atomic_write_json()` | `luno.response_depth_preference.DepthPreferenceStore.save()` | `luno.config.RESPONSE_DEPTH_PREFERENCE_FILE` | No | Yes | Yes | print() only |
| B7. `config/reminders.json` | `luno.persistence.atomic_write_json()` | `luno.reminders._save()` | `luno.config.REMINDERS_FILE` | No | Yes | Yes | print() only |
| C. `config/apps.json`, `lights.config.json`, `switches.config.json`, `scripts.config.json`, `persona.json`, `browser_monitor_targets.json`, `environment_triggers.json` | **none exists** | — | — | — | — | — | — |
| D. Browser downloads | `BrowserProvider.download()` (Playwright, via `RealBrowserHandler._dispatch()`) | tool call `browser`/`download` | `BrowserConfig.download_dir` (env, validated Sprint 66) | Filename only (validated) | No (browser-level write, not this project's atomic-write pattern) | No | none |
| E. Backup copies (`config/backups/*.json`) | `backup_current_file()` / `luno.memory._backup_current_memory_file()` | inside `atomic_write_json()`/`_save()`, before every write | same directory as the primary file + `backups/` | No | N/A (plain `shutil.copyfile`) | N/A (it IS the backup) | print() only |
| F. Tool-controlled writes | Browser `download` only (Sprint 65's own audit: no other tool handler opens a file for writing) | — | — | — | — | — | — |
| G. Atomic-write helpers | `luno.persistence.atomic_write_json()` (7 stores) + `luno.memory._atomic_write_json()` (1 store, private mirror of the same contract) | — | — | — | — | — | — |
| H. `os.replace()` | Inside both atomic-write helpers above | — | — | — | — | — | — |
| I. Rename/move | None found beyond H — no separate `os.rename()`/`shutil.move()` call site touches any state/config/critical path (confirmed via repo-wide grep; the only other `os.rename`-adjacent mentions are docstring prose) | — | — | — | — | — | — |
| J. Delete operations | `prune_backups()` / `luno.memory._prune_memory_backups()` (retention pruning of OLD backups only) | inside `atomic_write_json()`/`_save()`, after a successful write | `config/backups/` | No | N/A | N/A | print() only |
| K. Generated files (audio/whisper temp `.wav` scratch in `main.py`) | `tempfile.NamedTemporaryFile` + `os.remove()` | audio playback / STT pipeline | OS temp dir | No | N/A | N/A | Surveyed, deliberately NOT instrumented — ephemeral audio scratch, not project state; out of Phase 2's CRITICAL/STANDARD scope and orthogonal to this sprint's forensic goal |

**Row C confirms Sprint 65's own finding, re-verified rather than assumed**
(Phase 0/1's own explicit instruction: "do not assume a filename means a
writer exists"): `luno/devices.py`, `luno/environment_intent.py`, and
`luno/browser/config.py::load_monitor_targets()` were re-read this sprint
and confirmed to contain zero write-mode `open()`/`json.dump()` call
sites against any of those seven files. There is nothing to instrument
because nothing writes there.

## Phase 2 — Audit event schema

`luno.mutation_audit.MutationEvent` (dataclass, `luno/mutation_audit.py`):

```
timestamp, operation, path, path_category, source_component,
source_operation, success, before_exists, after_exists, before_size,
after_size, before_sha256, after_sha256, tool_name, action_name,
correlation_id, pid
```

Every field is fixed and named — there is deliberately no generic
`data`/`content`/`value`/`payload` slot (`tests/test_sprint67_mutation_
audit_trail.py::test_P_schema_has_no_generic_content_field` asserts
this structurally). Nothing in this module ever reads file CONTENTS,
memory contents, or conversation text — `snapshot()` only ever calls
`os.path.exists()`/`os.path.getsize()`/a SHA-256 digest, never
`open(path).read()`'s result into anything that gets stored. `source_
component`/`source_operation`/`tool_name`/`action_name` are bounded to
`MAX_FIELD_CHARS` (200) as defense in depth, mirroring `event_log_
writer.py`'s own `_bound_value()` — every real caller passes a short,
fixed, code-authored string (e.g. `"persistence"`/`"atomic_write_json"`),
never LLM-controlled text, so this bound is precautionary, not a
response to an observed overflow.

## Phase 3 — Hash strategy

- **CRITICAL** (`config/*.json` persistence/config stores, `.env`,
  launcher/manifest files — reuses Sprint 66's own `_collect_critical_
  paths()` inventory unchanged): SHA-256 always captured, before and
  after. These files are, by this project's own established evidence
  (Sprint 65/66), never observed above a few KB — hashing costs
  microseconds.
- **STANDARD** (browser downloads, backup copies): SHA-256 captured only
  when the file is at or under `STANDARD_HASH_MAX_BYTES` (10 MiB) —
  size + existence are always captured regardless. Above that threshold,
  only size/existence metadata is recorded (documented policy, not
  silently skipped).
- **TEMP** (retention-pruned backup deletions, anything outside the
  above two categories): metadata only (`exists`/`size`), never hashed —
  the lowest forensic priority this module tracks, and pruning can
  delete many files in one retention pass.

## Phase 4 — Audit storage

`logs/mutation_audit/YYYY-MM-DD.jsonl` — a new sibling directory next to
the SAME `logs/` root `luno/dashboard/event_log_writer.py` already
established for `logs/events/`/`logs/runtime/` (Sprint 50). Reusing this
existing, already-reviewed location (rather than inventing a new one)
satisfies every Phase 4 requirement directly: outside `luno/`, outside
the browser download root, outside `long_term_memory.json`, outside any
arbitrary config file, deterministic (one file per UTC day), and — the
requirement that actually needed new work — **not controlled by LLM/tool
arguments**: `AUDIT_LOG_DIR` is a module-level constant set once via
`os.getenv("MUTATION_AUDIT_LOG_DIR", "")` at import time (the same
trusted-config-only convention every `luno/config.py` `*_FILE` constant
uses), never read from a `ToolCall.parameters` value anywhere in this
codebase (`tests/test_sprint67_mutation_audit_trail.py::test_S_no_
toolcall_parameter_ever_reaches_audit_log_dir_selection` is an AST-based
proof, not an assumption).

## Phase 5 — Audit writer security

1. **Path is fixed/config-derived**: see Phase 4.
2. **LLM/tool arguments never select it**: see Phase 4 / `test_S_*`.
3. **Cannot be invoked as a generic tool**: `mutation_audit` is never
   registered as a `ToolHandler` (no such name exists in `luno.bootstrap.
   adapters`/`luno.tool_manager.builtin.__init__.register_all()`),
   confirmed by `test_R_mutation_audit_is_never_registered_as_a_tool_
   handler`.
4. **No arbitrary executable content**: no `eval()`/`exec()`/
   `__import__()`/`getattr()`-by-string/`globals()[...]` anywhere in
   `luno/mutation_audit.py` (`test_P_secret_shaped_strings_are_never_
   specially_extracted_or_executed`, `test_operation_field_has_no_
   dynamic_dispatch_table`).
5. **Deterministic serialization**: plain `json.dumps(..., default=str,
   ensure_ascii=False)` of a `dataclasses.asdict()` — no custom encoder,
   no non-deterministic ordering (dataclass field order is fixed).
6. **A `path` argument is metadata only, never a write target**: the
   ONLY write-mode `open()` call anywhere in `luno/mutation_audit.py`
   targets the module's own computed audit-file path
   (`os.path.join(_audit_dir(), f"{_today_str()}.jsonl")`) — never the
   caller-supplied mutation `path` (`test_record_mutation_never_opens_
   the_mutated_path_for_writing` is a structural, single-write-call-site
   proof, not an inspection of one example).

**Most importantly — fail-closed, but scoped correctly:**
`assert_audit_subsystem_available()` is the ONE function in this module
allowed to raise and block a real mutation. It is called by `luno.
persistence.atomic_write_json()` and `luno.memory._atomic_write_json()`
ONLY when the target path classifies CRITICAL, and ONLY BEFORE the
backup/write sequence begins. It re-validates the audit directory's own
safety invariant (reusing Sprint 66's `validate_download_directory()`
unchanged) and confirms the directory exists/is writable. If either
check fails, it raises `AuditSubsystemUnavailableError` — the caller's
write simply does not proceed. For `luno.memory._save()`, this raise is
absorbed by that function's own PRE-EXISTING "never raise out of a save"
catch-all (unchanged this sprint), so the practical effect is identical
to every other pre-existing `_save()` failure mode: the write is
skipped, logged, no crash. STANDARD-category mutations (browser
downloads) never call this function at all — a broken audit subsystem
can never block a legitimate download (Phase 5 only requires fail-closed
for security-sensitive/CRITICAL mutations, and Phase 2's own "don't make
the browser useless" precedent from Sprint 66 applies here too).

## Phase 6 — Atomic mutation integration

Both `luno.persistence.atomic_write_json()` and `luno.memory._atomic_
write_json()` were restructured so the audit record is built in a
`finally` block, AFTER the real `os.replace()` has already succeeded or
raised — never before. A `success` flag, initialized `False`, is only
set `True` immediately after `os.replace()` returns without raising.
Every pre-existing atomic-write guarantee (temp file in the same
directory, `fsync()`, `os.replace()` for cross-platform atomic
overwrite, best-effort `.tmp` cleanup on failure, original file
untouched until the final swap) is completely unchanged —
`tests/test_sprint67_mutation_audit_trail.py::test_G_atomic_write_
guarantee_is_unweakened_by_audit_integration` re-proves this holds with
the new hooks wired in.

## Phase 7 — `config/long_term_memory.json` forensic coverage

`luno.memory._atomic_write_json()` — the dedicated, private writer this
one file uses — now captures, for every FUTURE write:

1. **When** — `timestamp` (ISO-8601 UTC, millisecond precision).
2. **Previous SHA-256** — `before_sha256` (always computed; CRITICAL
   category is unconditional).
3. **New SHA-256** — `after_sha256`.
4. **Which component** — `source_component="memory"`.
5. **Which operation** — `source_operation="_atomic_write_json"`.
6. **Did it succeed** — `success` (only `True` after a real `os.replace()`).
7. **Atomic replacement** — implied by `path_category == "CRITICAL"` and
   this being the same temp-file+fsync+`os.replace()` sequence every
   other CRITICAL store uses; `_backup_current_memory_file()`'s own
   audit record additionally proves a backup preceded it.
8. **Was a backup involved** — a paired `operation="backup_create"`
   event, emitted by `_backup_current_memory_file()` (Sprint 67's own
   addition to that pre-existing function).

**The current, already-corrupted `config/long_term_memory.json` on disk
was NEVER read for content, rewritten, or otherwise touched by this
sprint.** `tests/test_sprint67_mutation_audit_trail.py::test_long_term_
memory_current_production_file_is_never_read_or_written_by_this_suite`
hashes and mtimes the real file before and after exercising the
instrumented code path against an isolated temporary copy, and asserts
both are unchanged. The production file's SHA-256
(`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a`)
matches every prior sprint's own recorded value since Sprint 55,
unchanged by this sprint.

## Phase 8 — Browser download coverage

`RealBrowserHandler._dispatch()`'s `"download"` branch (already hardened
by Sprint 66's `validate_download_directory()`/`validate_download_path()`
calls, both left completely unchanged) now wraps the actual
`p.download(url, resolved)` call with a before/after `snapshot()` and a
single `record_mutation(operation="download", ...)` call in a `finally`
block — recording success or failure either way, `tool_name="browser"`,
`action_name="download"`, and a per-call `correlation_id`. Downloaded
file CONTENTS are never read into the audit record (only size/hash/
existence). Sprint 66's path-validation calls execute exactly as before,
in the exact same order, before this new instrumentation runs — no
weakening of that boundary.

## Phase 9 — Tool correlation

No correlation-ID mechanism existed anywhere in `luno.tool_manager`
before this sprint (`ToolCall`/`ExecutionContext`/`ToolResult` — none
have such a field). Per the brief's own "add the smallest safe internal
mechanism, do not build a second tracing system" instruction: a single
`uuid.uuid4().hex[:16]` is generated locally, once, inside `RealBrowser
Handler._dispatch()`'s own `"download"` branch — scoped to that one
dispatch call, threaded into the one `record_mutation()` call it
produces. No change to `ToolCall`, `ToolManager`, or any other handler's
signature. This ID is generated by Luno's own executing code, never
accepted as (or derived from) an LLM-supplied string.

## Phase 10 — Failure/crash behavior — the evidence boundary

This module is an APPLICATION-LEVEL forensic log, not a tamper-proof
ledger — no cryptographic chaining, no host-OS write-once guarantee, no
protection against someone with filesystem access editing the JSONL
files directly. What IS proven, with tests:

- **A. Mutation succeeds + audit succeeds** — the common case; full
  before/after/success record. (`test_A`, `test_F`)
- **B. Mutation fails + audit succeeds** — `success=false` recorded,
  original file provably untouched. (`test_B`)
- **C. Audit write fails BEFORE mutation** (CRITICAL only) — the
  mutation is refused entirely (`AuditSubsystemUnavailableError`),
  original file untouched, consistent with `_save()`'s own pre-existing
  never-raise contract for `long_term_memory.json`. (`test_K_*`)
- **D. Audit write fails AFTER mutation** — the mutation has ALREADY
  succeeded (`os.replace()` already returned) by the time an audit
  append could fail; it cannot be retroactively blocked. This IS a
  genuine, honestly-documented forensic blind spot: the write happened,
  is real and permanent, but this one event goes unrecorded (counted in
  `mutation_audit.stats()['write_failures']`, and best-effort-logged to
  stderr). Not a security bypass — no capability was granted or denied
  incorrectly; the mutation would have happened identically without
  Sprint 67 in place. (`test_O`)
- **E/F. Process interruption before/after `os.replace()`** — covered by
  the SAME pre-existing atomic-write guarantee (Phase 6) this sprint
  left unweakened: before `os.replace()`, only a throwaway `.tmp` file
  is affected; after, the new content is durably in place (subject to
  the OS's own fsync/replace guarantees, unchanged from before this
  sprint).
- **G. Malformed audit event** (e.g. a truncated trailing JSONL line
  from a real crash mid-write) — `read_events_for_day()` skips any line
  that fails to parse rather than failing the whole read. (`test_M`)
- **H/I. Audit directory/file unavailable** — covered by C above for
  CRITICAL; for STANDARD/TEMP, `_append_event()` is fully best-effort
  and never raises regardless of cause.
- **J. Concurrent mutations** — see Phase 11.

## Phase 11 — Concurrency

A single `threading.RLock()` scoped ONLY to the audit-file append
operation (never a global application lock, per the brief's own
instruction) guards `_events_written`/`_write_failures` bookkeeping
around each `open(..., "a")` + `write()`. Each event line is far smaller
than the OS's own atomic-write guarantee for a single `write()` syscall
(`PIPE_BUF`, typically 4KB+), so even without the lock two writers could
not interleave mid-line on POSIX — the lock exists for this module's own
counters, not to prevent line corruption at the OS level.
`tests/test_sprint67_mutation_audit_trail.py::test_L_concurrent_writers_
never_corrupt_or_lose_events` spins 40 threads writing concurrently and
confirms exactly 40 valid, parseable events land in the file; `test_L_
rapid_sequential_writes_to_the_same_file` confirms 10 rapid sequential
writes to the SAME store each produce their own correctly-ordered event.

## Phase 12 — Retention

`rotate_old_audit_logs(max_retention_days=90)` — date-mtime-based
deletion of `logs/mutation_audit/YYYY-MM-DD.jsonl` files older than the
retention window, the same approach `event_log_writer.py::_rotate_old_
files()` already uses for `logs/events/`/`logs/runtime/`. 90 days
(longer than that module's own 14-day default) reflects this being the
forensic evidence trail itself, not a routine debug log. `max_retention_
days <= 0` disables rotation entirely rather than deleting everything
(an explicit escape hatch). Rotation can ONLY ever delete files under
this module's OWN `AUDIT_LOG_DIR` — it never touches `config/*.json`,
backups, or anything else (`test_N_retention_never_touches_config_json`
hashes every `config/*.json` file before and after a rotation call and
confirms zero drift). A real runtime process rotates opportunistically,
at most once per process, the first time it writes any event
(`_maybe_rotate_once()`) — negligible overhead; tests exercise the real
`rotate_old_audit_logs()` function directly rather than relying on that
one-shot gate.

## Phase 13 — Negative-control security tests

All implemented in `tests/test_sprint67_mutation_audit_trail.py`:
LLM/tool cannot choose the audit path (`test_S_*`, AST-based); cannot
redirect audit records into source code (`test_J_audit_dir_inside_
source_root_fails_its_own_safety_check`, `test_Q_*`); cannot disable
auditing through tool arguments (no such parameter exists anywhere in
the call chain — `record_mutation()`/`atomic_write_json()` accept no
"skip_audit" flag reachable from a `ToolCall`); cannot overwrite audit
configuration (`test_no_setter_function_exists_to_reconfigure_the_
audit_path_at_runtime` — no public setter exists at all, only the
module-level constant and test monkeypatching); cannot use the audit
writer as an arbitrary filesystem write primitive (`test_record_
mutation_never_opens_the_mutated_path_for_writing`); cannot inject
secrets into audit records (`test_P_*`); cannot modify protected files
merely by creating audit events (`test_config_json_untouched_by_a_
batch_of_audit_events`); cannot register a new audit operation
dynamically (`test_operation_field_has_no_dynamic_dispatch_table`);
unknown tool/action are unaffected by this sprint (Sprint 66's own
`error_type="unknown_tool"`/`validate()` rejection paths, untouched);
no registry mutation (`test_T_mutation_audit_module_never_touches_the_
tool_registry`).

## Test results

`tests/test_sprint67_mutation_audit_trail.py` — **48 tests, 0 failed.**

## Targeted regression

`test_sprint67_mutation_audit_trail.py` (48) + Sprint 63/64/65/66's own
test files (24+27+27+40=118) + full memory-suite batch (27 files, 1103
tests) + `luno/tool_manager/tests/` + `tests/test_browser_wiring.py` +
`tests/test_desktop_control.py` + `tests/test_relationship_engine.py` +
`tests/test_response_policy.py` + `tests/test_proactive.py` —
**1633 passed, 3 skipped, 0 failed** across the combined runs.

## Full repository regression

`python3 -m pytest tests/ -q --continue-on-collection-errors
--ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py
--timeout=60 -p no:cacheprovider` — **3374 passed, 28 failed, 3 skipped**
in 750s. Every failure matches the SAME file/test-name set every prior
sprint since Sprint 62/63 has already classified as full-suite-only
timing-interference or pre-existing environment-coupled flakiness
(`test_dashboard.py`, `test_emotion_engine.py`, `test_llm_dashboard.py`,
`test_llm_tts_streaming_production.py`, `test_mic_device_index.py` —
re-confirmed as the pre-existing `list_microphones.py`-absent
environment issue, not timing, `test_production_launcher.py`,
`test_real_adapters.py`, `test_runtime_demo.py`'s episodic-memory test,
`test_state_isolation.py`, `test_streaming_e2e.py`,
`test_streaming_speech_integration.py`, `test_tts_chunk_pipelining.py`,
`test_tts_e2e_pipeline.py`, `test_voice_pipeline_latency.py`). One
individual test not seen failing in Sprint 66's own run
(`test_llm_tts_streaming_production.py::test_13_cancellation_before_
first_audio`) was re-run in isolation and passed cleanly — same
already-documented file, same timing-interference class, not a new
regression. A representative sample (`test_llm_tts_streaming_
production.py::test_13_...`, `test_dashboard.py::test_35_...`,
`test_voice_pipeline_latency.py::test_A_...`, and the full `test_mic_
device_index.py` file) was individually re-run and confirmed: the three
timing-class tests pass in isolation; `test_mic_device_index.py`'s 4
failures reproduce identically in isolation and are the known,
pre-existing `list_microphones.py`-absent environment gap (unrelated to
this sprint's changes, documented since Sprint 55). Zero failures touch
`luno/persistence.py`, `luno/memory.py`'s save path, `luno/mutation_
audit.py`, or the browser download boundary.

## Persistent state

`config/*.json` (15 files) SHA-256-identical before this sprint's
deliberate code edits vs. after the full test/regression run, including
`config/long_term_memory.json` itself (unchanged since Sprint 55). A
dedicated critical-file hash set for this sprint (`ARCHITECTURE_
GUARD.md`, `luno/tool_manager/manager.py`, `luno/tool_manager/
registry.py`, `luno/desktop_control.py`, `luno/browser/security.py`,
`luno/browser/permissions.py`, `luno/browser/config.py`, `luno/tool_
manager/builtin/real_browser.py`, `luno/config.py`, `main.py`,
`main_runtime_demo.py`, `luno/memory.py`, `luno/persistence.py`,
`luno/mutation_audit.py`, `tests/conftest.py`) confirmed byte-identical
before/after. `config/backups/` count unchanged (12 entries, same as
before this sprint's test runs — no new production backup was created).
No real `logs/mutation_audit/` directory exists in the production
checkout (the test suite's own isolation, extended in `tests/conftest.py`
this sprint, redirected every write there during the full sweep) — it
is created only by real application usage going forward, exactly as
`logs/events/`/`logs/runtime/` already are.

## Performance

`record_mutation()` and `snapshot()` (SHA-256 of a small critical JSON
file) both measured well under the 5ms/operation target in dedicated
timed tests (200 iterations each, averaged). Neither makes a network
call, an LLM call, or unnecessary blocking I/O — pure path-string
computation, a stat, and (for CRITICAL/STANDARD-under-threshold) a
SHA-256 digest of a small file.

## Known limitations

This module cannot prove tamper-resistance against an actor with direct
filesystem access to `logs/mutation_audit/` itself — it is an
application-level log, not a cryptographically-chained or host-OS
write-once ledger (Phase 10's own honest framing). Post-mutation audit
append failure (Phase 10.D) is a genuine, accepted forensic blind spot,
not a security gap. Rotation runs opportunistically once per process
rather than on a dedicated schedule — a very long-lived process could
theoretically go a long time between rotation passes (bounded in
practice by normal process restarts). Windows-specific audit-directory
behavior was only provable structurally (this sandbox is Linux), same
limitation Sprint 66 already documented for its own path-safety
primitives, which this sprint reuses unchanged.

## Remaining forensic blind spots

Ephemeral audio/whisper temp-file writes (`main.py`'s `tempfile.
NamedTemporaryFile` scratch files) remain uninstrumented — surveyed in
Phase 1, deliberately out of scope (not project state, not
security-relevant, high-frequency enough that hashing them would be
pure overhead for zero forensic value). Chain G from Sprint 65/66 —
whether an external service (Home Assistant) could reach back into this
project's filesystem outside any path this sprint's own writers cover —
remains UNKNOWN, unchanged, out of scope for an application-level audit
trail. The exact root cause of `config/long_term_memory.json`'s
PRE-EXISTING corruption (Sprint 63/64's own subject) remains unsolved by
this sprint, exactly as intended — Sprint 67 only instruments FUTURE
mutations of that file, per Phase 7's own explicit "do not restore or
modify the currently-corrupted memory file" instruction.
