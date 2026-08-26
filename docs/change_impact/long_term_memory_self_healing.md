# LUNO — Long-Term Memory Self-Healing / Recovery Hardening

## 1. Objective

Harden the EXISTING Manual Long-Term Memory persistence system
(`config.LONG_TERM_MEMORY_FILE`, implemented entirely in `luno/memory.py`)
so that Luno can never fail to start merely because that one file is
corrupted or unrecoverable, without ever inventing a second memory store,
schema, backup system, or persistence abstraction. This is a reliability
patch, not a redesign — every other persistent store (Verified Facts,
Episodic Memory, Relationship State, Habit Memory, Reminders, Vision
Memory, Session Summaries) and every unrelated subsystem (retrieval
ranking, importance/usefulness scoring, conflict resolution, dedup,
memory budget, source priority) is untouched.

## 2. Architecture found (before any code was written)

`LONG_TERM_MEMORY_FILE` does **not** go through the generic
`luno/persistence.py` module that six other stores share — it has its
own private, parallel implementation inside `luno/memory.py`
(`luno/persistence.py`'s own docstring documents `luno/memory.py` as
"the reference implementation" it was extracted from, deliberately not
unified). This ruled out the obvious-looking option of extending
`luno/persistence.py` — doing so would not have touched
`LONG_TERM_MEMORY_FILE` at all.

The single hardest constraint found: `_load()` runs unconditionally at
**module import time** (`_load()  # load sekali saat modul pertama kali
diimpor`, bottom of `luno/memory.py`) — potentially before any test or
bootstrap isolation redirect has run. This is the exact root cause of a
real, previously-documented incident (`docs/change_impact/
memory_recovery.md`) where a bare, non-isolated import once silently
overwrote real production `config/long_term_memory.json`. An existing,
already-passing regression test
(`tests/test_sprint64_memory_corruption_forensics.py::
test_B_load_is_read_only_no_write_primitive_in_its_source`) enforces —
at the source-text level — that `_load()` contains zero write
primitives. Any design that made `_load()` itself perform the
quarantine-copy/fresh-store write (as the brief's own pseudocode
initially suggested) would have violated this already-tested guarantee
and reintroduced exactly the risk this whole hardening layer exists to
prevent.

The second constraint found: `tests/test_manual_memory.py::
test_partial_malformed_entries_are_skipped_not_crashed` is an existing,
intentional, passing test proving that a primary file with one
well-formed entry and one malformed entry must keep the good entry
retrievable — entry-level malformation is tolerated by design, not a
recovery trigger. Rejecting the whole file on any single bad entry
would have silently discarded the still-good sibling, a strictly worse
outcome and a direct regression of preserved behavior.

## 3. Design

Both constraints resolved via one shape: **`_load()` stays 100%
read-only.** On unrecoverable corruption it only decides, in memory,
that a quarantine-and-fresh-store is needed (`_pending_quarantine_path`)
— the actual disk-side quarantine copy is deferred to the next real
`_save()` call, the one existing write funnel every other mutation
already goes through. Validation (`_validate_memory_data()`) checks
**root shape only** (`isinstance(data, list)`) for both the primary and
every backup candidate — matching, not weakening, the existing
entry-level-tolerant behavior.

### Recovery sequence (`_load()`)

1. Primary missing → healthy, empty store (unchanged; never creates the
   file as a side effect).
2. Primary reads and validates → healthy, loaded as-is (unchanged).
3. Primary fails to read/parse OR fails root-shape validation →
   `_recover_from_backup_or_go_fresh()`: scan existing backups
   newest-first (`_load_latest_valid_backup()`, unchanged mechanism, now
   sharing the same validation contract as the primary); first valid one
   wins, loaded byte-for-byte as-is (ids/metadata never re-ranked or
   rewritten) → status `"recovered_from_backup"`. The corrupted primary
   is left completely untouched on disk; the next successful `_save()`
   naturally persists the recovered content back via the existing,
   already-hardened atomic-write mechanism.
4. Primary invalid AND no backup usable either → status
   `"fresh_after_unrecoverable_corruption"`, `_memories` becomes `[]`
   (the existing fallback value), and `_pending_quarantine_path` records
   the corrupted primary's path for the next `_save()` to quarantine.

### Quarantine (`_finalize_pending_quarantine_if_any()`, called from `_save()`)

A **copy** (`shutil.copy2`), never a move — `_save()`'s own subsequent
backup-then-atomic-write handles the primary path normally afterward.
Lands in `<same dir>/quarantine/` — a new sibling of the existing
`backups/` directory, deliberately separate so a known-corrupt file can
never be picked up by `_list_memory_backups()`'s own glob and mistaken
for a legitimate backup. Named
`long_term_memory.corrupt.<timestamp>.json`; a numeric suffix
(`.1`, `.2`, …) is appended on the astronomically unlikely event of a
same-microsecond collision, so an existing quarantine artifact is never
overwritten. A quarantine failure (permissions, disk full) is caught
narrowly (`except OSError`), logged, and swallowed — the fresh-memory
save that follows is still attempted, per "Luno must not crash solely
because quarantine failed."

Stale-pending-path defense: `_pending_quarantine_path` is unconditionally
reset to `None` at the very start of every `_load()` call, and
`_finalize_pending_quarantine_if_any()` additionally verifies the
pending path's directory still matches `config.LONG_TERM_MEMORY_FILE`'s
current directory before acting — a stale value left behind by an
earlier, unrelated `_load()` (e.g. a different test's own `tmp_path`)
can never leak into a later `_save()`.

### Observability

Reused the existing, in-memory, non-dashboard mechanism pattern — no new
state model, no new dashboard page. `_persistence_status` (module-level
dict, `{"status": ..., "detail": ...}`) is one of `"healthy"`,
`"recovered_from_backup"`, or `"fresh_after_unrecoverable_corruption"`,
readable via `get_persistence_status()` (returns a copy) and surfaced
through the existing `memory_health_report()`'s return dict as one new
key, `"persistence_status"`. Never persisted inside the memory data
itself. Log lines only ever mention filenames/status/reason — never
memory contents.

## 4. Error handling

Only `(OSError, ValueError, UnicodeDecodeError)` are treated as
persistence-corruption/recovery conditions — the existing narrow
exception classes already used by `_load_latest_valid_backup()`, now
also used by `_load()`'s own read attempt. Quarantine's own
`shutil.copy2` failure is caught as `except OSError` only. No
`except Exception: pass` was added anywhere; unexpected programming
errors remain visible.

## 5. Concurrency / atomicity

No new locking or distributed-transaction mechanism. The existing
guarantees are preserved and reused as-is: backup-before-write
(`_backup_current_memory_file()`), atomic tmp+fsync+`os.replace()`
(`_atomic_write_json()`), retention pruning
(`_prune_memory_backups()`), and the pytest non-isolated-write guard
(`_refuse_if_pytest_targeting_unisolated_path()`) — all unmodified.
Quarantine finalization is idempotent and at-most-one-attempt per
pending path (cleared immediately on entry to
`_finalize_pending_quarantine_if_any()`), so repeated `_save()` calls
after a single recovery event never quarantine more than once.

## 6. Files changed

- `[MODIFIED] luno/memory.py` — new constants/docstrings, new
  `_validate_memory_data()`, `get_persistence_status()`,
  `_memory_quarantine_dir()`, `_memory_quarantine_filename()`,
  `_recover_from_backup_or_go_fresh()`, and
  `_finalize_pending_quarantine_if_any()`; `_load()` rewritten to the
  4-branch recovery sequence above (still 100% read-only);
  `_load_latest_valid_backup()`'s validation now shares
  `_validate_memory_data()`; `_save()` gained exactly one new call
  (`_finalize_pending_quarantine_if_any()`), placed after the existing
  pytest guard and before the existing backup/write/prune sequence;
  `memory_health_report()` gained one new return key,
  `"persistence_status"`. No other function in this 5,700+-line file
  was touched.
- `[MODIFIED] tests/test_memory_persistence_hardening.py` — 23 new test
  functions appended after the file's existing 11 tests (all 11
  preserved, unmodified, still passing), covering all 26 brief-mandated
  scenarios (several scenarios share one test function where the brief's
  own numbering was finer-grained than the natural test boundary, e.g.
  #20/#21/#22 — "does not touch Verified Facts / Episodic Memory /
  Relationship State" — verified together in one test since they are
  one assertion pattern applied to three files). Test #10 ("invalid
  individual memory entry → recovery") is written to *document* the
  deliberate root-shape-only boundary rather than contradict the
  existing, preserved `test_partial_malformed_entries_are_skipped_not_
  crashed`, plus a companion test proving the one part of "invalid
  recovery source is rejected" that IS enforceable: a backup candidate
  with the wrong root shape is skipped in favor of an older valid one.
- `luno/persistence.py`, `luno/config.py`, `tests/conftest.py`,
  `luno/mutation_audit.py`, dashboard code — inspected, **not modified**
  (no change was technically necessary; `LONG_TERM_MEMORY_FILE` never
  routes through `luno/persistence.py`, and the existing
  `isolate_persistent_state`/`mutation_audit` fixtures already cover
  the new code paths without changes).

## 7. Tests

`tests/test_memory_persistence_hardening.py`: **34 passed** (11
pre-existing + 23 new), 0 failed.

## 8. Regression

Targeted memory sweep (28 memory-related test files, run before the full
sweep): 19 pre-existing failures, 1094 passed — all 19 independently
triaged and confirmed pre-existing/unrelated (see §9).

Full repository sweep (139 collectible files; the same 2 files —
`tests/test_main_bargein.py`, `tests/test_root_main_bargein.py` — fail
to *collect* for pre-existing, environment-specific reasons unrelated to
this sprint, same as every prior sprint in this project's history):

**4,075 passed, 37 failed, 1 skipped.**

Compared to the last recorded full-suite baseline (P0.8.2,
`docs/testing/regression_baseline.md`): **4,052 → 4,075 passed (+23,
exactly the number of new tests added this sprint), 37 failed → 37
failed (unchanged), 1 skipped → 1 skipped (unchanged). Zero new
failures anywhere in the repository.**

## 9. Failure triage (all 37, all pre-existing)

- **7** — `tests/test_llm_max_completion_tokens_compatibility.py`: a
  pre-existing `max_tokens` vs `max_completion_tokens` API-parameter
  mismatch in the LLM adapter layer. Confirmed unrelated by direct
  inspection: `luno/memory.py`'s only `max_tokens=150` call site
  (line 2380, inside `summarize_and_archive_session()`) is a completely
  separate function this sprint never touched.
- **5** — `tests/test_memory_session_summary_api_compatibility.py`: the
  exact same root cause as above, surfacing through
  `summarize_and_archive_session()`. Confirmed this sprint's changes
  cannot be the cause: `SESSION_SUMMARIES_FILE` persistence goes through
  `luno/persistence.py`'s generic `atomic_write_json()`/`safe_load_json()`
  (grep-confirmed), never the private `_load()`/`_save()` this sprint
  modified.
- **6** — `tests/test_mic_device_index.py`: pre-existing, references a
  `list_microphones.py` root script that isn't present in this
  checkout/sandbox, and a stale cross-session path
  (`/sessions/ecstatic-adoring-goldberg/...` vs. this session's own
  `/sessions/lucid-dazzling-darwin/...`) baked into a prior test-authoring
  session's `__file__`-based path — audio/microphone-device code,
  untouched.
- **3** — `tests/test_real_adapters.py` /
  `tests/test_production_launcher.py`: pre-existing
  `RealWhisperSource` test-construction gap (`_device_index` never set
  on a bare `__new__()`-constructed instance) and a health-check
  default-mode assertion — speech/launcher code, untouched.
- **16** — `tests/test_sprint63_long_term_memory_recovery.py` (9),
  `tests/test_sprint64_memory_corruption_forensics.py` (5),
  `tests/test_sprint68_mutation_audit_hardening.py` (2): forensic tests
  that hardcode a specific historical snapshot of the real
  `config/long_term_memory.json` (byte size, permission mode, a
  specific corrupted-content hash) and the real `config/backups/`
  directory's exact file count, both of which have organically evolved
  across many real sessions since those snapshots were recorded (the
  production file is now valid content — the same 5 entries already
  documented as "recovered" in `docs/change_impact/memory_recovery.md`
  — and `config/backups/` has accumulated 51 files with timestamps
  spanning Aug 11–20, none from this session). This exact failure
  category, including the same file count drift, was already documented
  in `ARCHITECTURE_GUARD.md` §93 (P0.8.2) before this sprint began.
  Independently reconfirmed here: this sprint's own tests never write to
  `config/backups/` or the real `config/long_term_memory.json` (all use
  `monkeypatch` + `tmp_path`), and the real file's hash is confirmed
  byte-identical before and after the entire test run (§10).

## 10. Production state safety

All 7 mandated persistent-state files hashed before any code was written
and again after the full regression sweep completed. Identical in every
case:

| File | SHA-256 |
|---|---|
| `config/long_term_memory.json` | `48edaf4a...81937a` (unchanged) |
| `config/verified_facts.json` | `76f690d2...fb85b4` (unchanged) |
| `config/episodic_memory.json` | `37517e5f...985b570` (unchanged) |
| `config/relationship_state.json` | `86fb5289...25fdf0` (unchanged) |
| `config/session_summaries.json` | `7e7e6c34...327ba` (unchanged) |
| `config/habit_memory.json` | `09ff3b1a...909d8` (unchanged) |
| `config/reminders.json` | `8252841a...077a8b2056` (unchanged) |

## 11. Acceptance criteria — verified, not assumed

- Luno never fails to start due to corrupted Manual Long-Term Memory —
  proven by `test_r3`, `test_r6`, `test_r7`, `test_r8b`, `test_r9`
  (all assert `_load()` does not raise and leaves a usable, empty
  `_memories`).
- Newest-valid-backup wins, deterministically — `test_r4`, `test_r5`,
  `test_r10b`.
- Corrupted primary is quarantined, never destroyed — `test_r11`
  (bytes preserved exactly), `test_r12` (never overwrites a prior
  quarantine artifact).
- Fresh store uses the exact existing schema and is immediately usable
  — `test_r15`, `test_r16`, `test_r17`.
- Recovery status is observable via the existing mechanism only —
  `test_r18`, `test_r19` (also proves `memory_health_report()`
  passthrough and copy-not-reference semantics).
- Unrelated stores (Verified Facts, Episodic Memory, Relationship
  State) are provably untouched by a full recovery cycle — `test_r20_
  r21_r22`.
- Repeated/idempotent recovery never corrupts the resulting file —
  `test_r26`.
- `_load()`'s read-only guarantee and `_save()`'s single-atomic-write
  guarantee (both pre-existing, source-text-enforced tests) still pass
  unmodified.

## 12. Known issues

None newly introduced by this sprint. The 37 pre-existing failures
above are all independently reproducible on file/test areas this
sprint's diff never touches, and were already partially documented
before this sprint began (§93 of `ARCHITECTURE_GUARD.md`).
