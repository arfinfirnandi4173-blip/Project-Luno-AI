# Mutation Audit Trail Verification & Hardening (Sprint 68)

**Type:** Verification/hardening of Sprint 67's forensic audit trail. Adds
no new capability class beyond what Sprint 67 already introduced - no
second persistence system, no second tracing system, no generic
filesystem writer, no LLM-controlled audit destination, no new
execution path. Every claim below was checked against the actual
checkout, not assumed from Sprint 67's own report.

## Why this sprint exists

Sprint 67 built `luno/mutation_audit.py` and integrated it into
`luno/persistence.py`, `luno/memory.py`, and the browser download path.
Sprint 68's brief explicitly required treating Sprint 67's own report as
unverified and re-deriving every claim from the actual source. That
re-derivation found one real gap and answered one open question the
Sprint 67 report had left unresolved.

## Genuine verification finding

Re-reading `luno/mutation_audit.py::record_mutation()` (not assumed from
the Sprint 67 writeup) showed the stored `path` field was `str(path)`
verbatim - **not** canonicalized, despite the architecture implying it
should be. Fixed with a new `_canonicalize_for_storage()` function using
`os.path.abspath()` - deliberately the WEAKER of this project's two path
functions (not Sprint 66's `_resolve_for_comparison()`, which resolves
symlinks and normalizes case for SECURITY comparisons). This is a
storage/display concern, not a security boundary: the audit record
should be human-readable and stable, and must never silently follow a
symlink when writing what will become a forensic record of what path was
actually named at the time of the operation. Never touches the
filesystem, never raises (falls back to `str(path)` on any exception).

## Changes

**`luno/mutation_audit.py`:**
- `_canonicalize_for_storage(path)` - new, described above. Used by
  `record_mutation()`'s `path=` field.
- `_bound()` extended with a `max_chars` parameter (previously hardcoded
  to `MAX_FIELD_CHARS=200`). `operation` is now bounded to 100 chars,
  the canonicalized `path` to a new `MAX_PATH_CHARS=4096`, and
  `correlation_id` to 64 chars - closing a defense-in-depth gap where
  only 4 of 7+ string fields were bounded against adversarial/oversized
  values.
- `_warn_if_audit_dir_unsafe_at_import()` - new, called once at module
  import time. Prints a non-fatal warning if `AUDIT_LOG_DIR` (which can
  be overridden by the `MUTATION_AUDIT_LOG_DIR` env var) fails the same
  safety check `assert_audit_subsystem_available()` uses. Deliberately
  NOT a raise: `mutation_audit.py` is imported transitively very early
  (by `luno.persistence`/`luno.memory`), so a hard failure here would
  crash unrelated application startup paths on a misconfigured env var.
  Real enforcement is unchanged from Sprint 67 - it still happens at
  actual CRITICAL-write time via `assert_audit_subsystem_available()`.
  This answers Phase 3's "env var cannot silently weaken the boundary"
  requirement: the boundary was never weak (a misconfigured directory
  still fails closed at write time), but it was previously silent until
  the first real write attempt - now it is visible at process start too.
- `record_pending_mutation()` - new. The Phase 6 hardening, described
  below.

**`luno/mutation_audit_replay.py`** - new module, strictly read-only
(`load_events`, `filter_by_path/correlation_id/source_component`,
`order_chronologically`, `count_malformed_lines`,
`find_orphaned_pending_events`, `summarize`). See "Replay helper" below.

**`luno/persistence.py::atomic_write_json()`** and
**`luno/memory.py::_atomic_write_json()`** - both now call
`mutation_audit.record_pending_mutation()` immediately after
`assert_audit_subsystem_available()` succeeds (before backup/write
begins) for CRITICAL-category paths, and thread the returned
`correlation_id` into the subsequent `record_backup_created()`
(persistence.py only) and final `record_mutation()` calls. `luno/
memory.py::_backup_current_memory_file()`'s own separate backup-audit
call was NOT threaded with this correlation_id - a scoped, deliberate
decision to avoid a more invasive restructuring of `_save()` for a
sprint whose brief explicitly warns against over-reaching.

## Failure-window hardening (Phase 6)

Sprint 67 documented an accepted blind spot: a mutation can succeed
while its own "it succeeded" audit record fails to append (disk full at
exactly the wrong moment, etc.) - the mutation is not retroactively
undoable, and the STOP CONDITIONS explicitly forbid building a second
persistence/transaction system to guarantee this can never happen.

Sprint 68's brief asked directly: can the existing implementation
**safely improve** this without violating architecture? Yes, via a
two-phase append using the SAME single append-only JSONL mechanism
Sprint 67 already built - not a second one. `record_pending_mutation()`
writes a `"<operation>:pending"` record before the mutation begins, with
`before` as a placeholder `after` and `success=False` as a placeholder
(both explicitly meaningless on pending records - the `:pending` suffix
is the only real signal). Its returned `correlation_id` is threaded into
the eventual completed record. If the completed append then fails, the
pending record survives, unmatched - a **detectable orphan**, not a
silent gap.

This does **not close** the blind spot - a mutation can still succeed
while every audit append about it fails, and no code can prove that
never happens without a second transaction system, which the brief's own
STOP CONDITIONS forbid. It converts an invisible gap into a discoverable
one for anyone doing forensic review afterward.

## Replay helper (Phase 8)

Justified specifically because the pending/completed pairing above needs
a consumer to actually detect orphans - reading raw JSONL by hand to
find an unmatched `"write:pending"` line is exactly the kind of
mechanical task Phase 8 anticipated. `luno/mutation_audit_replay.py`:
loads events across a day range (reusing, not duplicating, Sprint 67's
own `read_events_for_day()`), filters by path/correlation ID/source
component, orders chronologically, counts malformed lines, finds
orphaned pending events, and produces a small summary rollup. Strictly
read-only - proven structurally (AST inspection: zero write-mode
`open()` calls, zero `os.remove()`/`os.replace()`/`os.rename()` calls
anywhere in the module), not just by convention.

## Path trust boundary (Phase 3)

No second path-validation implementation was created. `classify_path()`
and `_validate_audit_dir()` continue to reuse Sprint 66's
`_collect_critical_paths()`/`_resolve_for_comparison()`/`_path_contains()`/
`validate_download_directory()` unchanged. Verified (with executable
tests, not assumed): the audit directory cannot be redirected into
`config/`, into `luno/` source, or into any path overlapping a critical
file; a symlink inside the audit directory pointing at a critical file
can never cause that file to be deleted by rotation (rotation's
`os.remove()` unlinks the symlink itself - POSIX semantics - never the
target); an unsafe `AUDIT_LOG_DIR` still fails closed for CRITICAL
writes and is still non-blocking for STANDARD downloads, unchanged from
Sprint 67.

## Concurrency (Phase 5)

30+ mixed writers (real persistence writes, memory writes, browser-style
audit events, concurrent rotation) produce complete, non-interleaved
JSONL lines, at most one completed-success event per successful
mutation, and unique correlation IDs. No second global state mechanism
was introduced - `luno/mutation_audit.py`'s only mutable module state is
the same `_lock`/`_events_written`/`_write_failures`/
`_rotation_attempted` counters Sprint 67 already had, verified by an
inventory test that fails if any new mutable global appears.

## Retention (Phase 7)

90-day, mtime-based rotation (unchanged from Sprint 67) verified against:
old-file deletion, current-day preservation, malformed filenames
(no crash), unrelated non-`.jsonl` files (never touched), a symlink
inside the audit directory (only the link is removed, never a critical
file it points at), future-dated filenames with a current mtime (kept -
retention is mtime-based, not filename-based), and retention pointed at
a directory that doesn't exist (returns 0, never raises). A dedicated
test proves rotation cannot delete `config/*.json`, `config/backups/`,
or this project's own source files even when pointed at real
`PROJECT_ROOT`/`SOURCE_ROOT` paths as part of the same sweep.

## Self-modification / security recheck (Phase 9)

Re-ran Sprint 65/66's own conclusions specifically against every Sprint
67 AND Sprint 68 addition: no `eval`/`exec`/`shell=True`/dynamic
`importlib`/subprocess command construction anywhere in `luno/mutation_
audit.py` or `luno/mutation_audit_replay.py`; zero new tool names
registered anywhere; the tool registry is never touched by anything this
sprint or Sprint 67 added; a simulated malicious `ToolCall.parameters`
dict cannot reach `operation`/`source_component`/`source_operation`/the
audit path anywhere in the real call graph (AST-verified, not just
tested at one call site); audit log content can never cause code
execution when read back (it is parsed as JSON and only ever displayed
or compared as strings/dicts, never `eval`'d or interpolated into a
shell/SQL/Python-exec context anywhere in `mutation_audit_replay.py`).

## Root cause found and fixed: a pre-existing, unrelated test-suite bug

While running Phase 12's full-repository regression, two of this
sprint's own new retention tests failed - but **only** in full-suite
runs, never in isolation, never in a ~300-test focused rerun, and a
first attempted fix (retrying the flaky operation with a bounded delay
loop) did not resolve it, which was the signal that this was not a
timing race. Adding a diagnostic to the failing assertion revealed
`time.time()` was returning `1000006.0` (about 11.6 days after the Unix
epoch) instead of a real 2026 timestamp inside these tests.

Root cause, confirmed by direct reproduction: `tests/test_camera_
presence.py`'s `_adapter()` helper did `vmod.time.time = lambda: ...` -
a raw attribute assignment on the *shared* stdlib `time` module object
(`vmod` is `luno.adapters.vision`, whose own `import time` is the exact
same module object every other file's `import time` gets), with no
corresponding restore anywhere in that file. Once any test in that file
ran, the real `time.time()` was gone for the rest of the pytest
process - not just for that file, and not just briefly. `tests/
test_camera_presence.py` sorts alphabetically before `tests/test_
sprint68_mutation_audit_hardening.py`, so this sprint's own retention
tests (the only ones in this file that call unmocked `time.time()`) were
downstream victims, not the cause.

**Fixed at the source**, not papered over: `tests/test_camera_
presence.py` gained one autouse fixture (`_restore_real_time_time`) that
restores the real `time.time` after every test in that file, regardless
of which test function triggered the patch. Confirmed by direct
before/after reproduction: `pytest tests/test_camera_presence.py tests/
test_sprint68_mutation_audit_hardening.py` failed 2/76 before the fix,
passed 76/76 after. This also explains why 20+ OTHER previously-flagged
"timing flakiness" tests (`test_dashboard.py`, `test_emotion_engine.py`,
`test_llm_tts_streaming_production.py`, `test_streaming_e2e.py`,
`test_streaming_speech_integration.py`, `test_tts_chunk_pipelining.py`,
`test_tts_e2e_pipeline.py`, `test_voice_pipeline_latency.py`) stopped
failing once this was fixed - they had been silently absorbing the same
frozen-clock corruption for an unknown number of prior sprints, always
misclassified as environment/timing flakiness because it never
reproduced outside a full-suite run. This is a test-infrastructure fix
only; zero production code (`luno/`) was touched by it, and it is
unrelated to this sprint's own mutation-audit work.

## Test suite

New file `tests/test_sprint68_mutation_audit_hardening.py` - 67 tests
covering schema-field verification, path trust boundary, adversarial
JSONL integrity (8 parametrized adversarial value types), 30-thread
concurrency, the 6 crash/failure-window scenarios from Phase 6, retention
(7 tests), the replay helper (5 tests, including strict read-only-ness),
self-modification/security recheck (7 tests), persistent-state
protection (4 tests, including a hardcoded backup-count invariant), and
performance (4 tests). 0 failed after the fixes above.

## Regression

- Sprint 68's own 67 tests: 67 passed, 0 failed.
- Sprint 67's 48 tests: 48 passed, 0 failed.
- Sprint 65/66's 67 tests: 67 passed, 0 failed.
- Memory/persistence suite (`-k "memory or persist"`, 1247 tests): 1244
  passed, 3 skipped, 0 failed.
- Browser/tool-manager suite (96 tests): 96 passed, 0 failed.
- Runtime/dashboard suite (244 tests): 243 passed, 1 failed
  (`test_llm_dashboard.py` - no local LLM server reachable from this
  sandbox, the same pre-existing environment gap documented since
  Sprint 61).
- **Full repository sweep** (`pytest tests/ -q --ignore=tests/test_
  main_bargein.py --ignore=tests/test_root_main_bargein.py --timeout=60
  --timeout-method=signal`, 3472 collected): **3460 passed, 9 failed, 3
  skipped**, 454s. Every failure re-run individually: 8 reproduce even
  in isolation, all matching the identical, long-documented environment
  gap (`test_mic_device_index.py` ×4, `test_real_adapters.py` ×2,
  `test_production_launcher.py::test_07` ×1, `test_llm_dashboard.py` ×1
  - missing audio hardware / no local LLM or speech server reachable
  from this sandbox); the 9th (`test_state_isolation.py`) passed cleanly
  alone, matching its own long-documented order-dependent-flake
  classification. **Zero failures touch `luno/mutation_audit.py`,
  `luno/mutation_audit_replay.py`, `luno/persistence.py`, or `luno/
  memory.py`'s save path.** This is a dramatically cleaner result than
  the 28-29 full-suite failures seen in three earlier attempts this
  sprint - see "Root cause found and fixed" above for why.

## Performance

All four dedicated performance tests passed under their targets: a
single audit event, 20 concurrent audit events, parsing 300 JSONL lines,
and a 50-file retention scan - each comfortably under 5-100ms depending
on the operation's own realistic budget (bulk/concurrent operations were
given a looser, explicitly-stated threshold than the single-operation
5ms target, per the brief's own "measure actual behavior" instruction).

## Persistent state

`config/*.json` (15 files) SHA-256-identical from before this sprint's
code edits through the end of the full regression sweep, including
`config/long_term_memory.json` itself
(`be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a` -
unchanged since Sprint 55, never read for content or written to at any
point this sprint). `config/backups/` count unchanged (12). The 6
production files this sprint edited/created (`luno/mutation_audit.py`,
`luno/persistence.py`, `luno/memory.py`, `luno/mutation_audit_replay.py`,
plus the unchanged-but-reverified `luno/tool_manager/builtin/real_
browser.py` and `tests/conftest.py`) confirmed byte-identical between
"edits finalized" and "after the full regression sweep completed".

## Known limitations (unchanged from Sprint 67 unless noted)

- The post-mutation audit-append-failure blind spot is now **detectable**
  (via orphaned pending records), not closed - closing it fully would
  require a second transaction system, which the STOP CONDITIONS
  explicitly forbid building.
- Application-level forensic log, not cryptographically tamper-proof.
- Windows-specific audit-directory behavior only provable structurally
  (Linux sandbox), same limitation Sprint 66/67 already documented for
  the reused primitives.
- `long_term_memory.json`'s pre-existing corruption (from before Sprint
  55) remains unsolved by design - this sprint, like Sprint 67, only
  instruments FUTURE mutations.
- The `tests/test_camera_presence.py` fix resolves a real, provable
  full-suite-only false-failure source, but it was not exhaustively
  proven that no OTHER file has a similar un-restored global-state leak
  - only this one was found and fixed, because it was the one blocking
    this sprint's own clean regression.

See `docs/change_impact/mutation_audit_trail.md` (Sprint 67) for the
audit trail's original architecture.
