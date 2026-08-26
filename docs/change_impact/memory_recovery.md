# Change Impact Analysis - Memory Recovery & Persistence Hardening

## 0. What happened (honestly, in order)

While doing ad-hoc manual verification of in-progress Memory Decision
Quality & Adaptive Retrieval sprint code (checking that the new
context-sensitive ranking logic worked), a `python3 -c "..."` one-liner
was run directly against this checkout, OUTSIDE of pytest and outside
any isolation mechanism. That script called `luno.memory._memories.clear()`
followed by `add_memory()`/`record_outcome_evidence()` calls. Because it
imported `luno.memory` directly (which loads `config/long_term_memory.json`
at import time and writes back to that same real path on every mutating
call), it overwrote Vinn's real, production `config/long_term_memory.json`
with 2 throwaway smoke-test entries.

This was caught immediately (before any further work), and a read-only
recovery audit was performed before touching anything further. No
newer backup than a 2026-07-23 snapshot bundled in `Luno Evo.zip` was
found anywhere reachable from this project. Vinn was asked to check
Windows File History / OneDrive / Recycle Bin / editor local history on
his own machine (outside this sandbox's visibility) - none of those
searches turned up anything newer.

## 1. Data loss - stated plainly

**Memory content between 2026-07-23 and 2026-08-09 cannot be recovered
from any source available to this project.** Whatever manual memories
existed, were edited, or were deleted in that ~2.5-week window are
gone. The only recoverable data is the 5 memories present in the
2026-07-23 snapshot.

- Damaged-state SHA256 (the 2 smoke-test entries that overwrote
  production): `5832067433f8bfa2fa7630d8840c094d7dd1a846bd61688cd0e7096efee491e5`
- Last-known-good baseline SHA256 (captured, but never read/printed,
  during the prior sprint's own Phase 2 baseline - hash alone is not
  enough to reconstruct content): `1b0de9394d85a937e302f9f63a4bb73ea4f0371a37e31cd43561283a47a1a9f0`,
  mtime 2026-08-09T00:13:20
- 2026-07-23 snapshot SHA256 (the recovery source): `7aa35cc65894d9dc841c963f590b7685a33b8dcedfbe5e641ee66524ba52bfec`

No fabrication was performed anywhere in this recovery: no invented
memories, no guessed importance/usefulness/feedback/evaluation/
retrieval-count/conflict history for the 5 recovered memories, and no
attempt to reverse-engineer what existed between 7/23 and 8/9.

## 2. Recovery source and audit

Only one internal snapshot exists: `Luno Evo.zip -> config/long_term_memory.json`
(2026-07-23 22:24, 5 entries, pre-`schema_version` format - bare
`id`/`text`/`created_at` only, no `category`/`importance`/`history`/
`source`/any evidence or evaluation field). A read-only audit (before
this sprint - see the prior turn's recovery audit) checked every
reachable location inside this project: the zip itself, an already-
extracted copy under `outputs/worldmodel_extract/` (byte-identical to
the zip's own entry, not an independent source), and all 617
`.fuse_hidden*` artifacts in `config/` (inspected by content, not just
name - every one is a 32KB binary blob matching `vision_memory.sqlite3`'s
WAL page shape, not JSON, not memory-shaped). No `.bak`/`.old`/`.orig`
files, no `.history`/`.vscode` folders, no other archives exist
anywhere in the tree. Windows File History/OneDrive/Recycle Bin/editor
local history are outside this sandbox's reach entirely.

## 3. Migration (2026-07-23 snapshot -> current schema)

`recovery/migrate_snapshot.py` is a one-time, standalone script that
deliberately does NOT import `luno.memory` (to guarantee it cannot, by
construction, touch live production state) - it copies the deterministic
category classifier verbatim from `luno/memory.py` rather than importing
it, documented in its own module docstring.

Per entry, exactly three fields are carried forward because they are
KNOWN, real facts from the snapshot: `id`, `text` (exact), `created_at`
(exact). One field is computed deterministically from the (known) text:
`category`, via the SAME keyword classifier every current memory already
uses - not a guess about 2026-07-23 intent, a mechanical classification
of text that is fully known. Three fields are set to the CURRENT
schema's own documented default for "unspecified" (never a claim about
history): `schema_version=4` (describes this migration's own output
format), `history=[]` (the schema's own "no history" representation),
`source="user_explicit"` (`add_memory()`'s own default parameter value).

Every OTHER field (`importance`, `usefulness_score`,
`positive_feedback_count`, `negative_feedback_count`, `retrieval_count`,
`last_retrieved_at`, `retrieval_success_count`, `retrieval_miss_count`,
`feedback_event_count`, `correction_count`, `conflict_event_count`,
`evaluation_score`, `last_evaluated_at`, `context_evidence`,
`conflict_status`, `conflict_group`, `updated_at`) is DELIBERATELY
OMITTED - not set to 0/None/a guessed value. Every one of those fields
already has a documented, tested, neutral-default accessor in
`luno/memory.py` for exactly this situation (a pre-Memory-Intelligence-
sprint / schema-v1-shaped entry that simply lacks the key) - `importance`
in particular is recomputed FRESH, on demand, every time, from
text/category (see `_get_importance()`'s own docstring), never trusted
from a persisted guess. Omitting is the more conservative choice than
persisting a computed-but-look-alike value, and it is not a gap - it is
this codebase's own pre-existing backward-compatibility path, applied
honestly rather than invented for this recovery.

Result (`recovery/migrated_candidate.json`, SHA256
`e4a92097eb920e74b495b1bef05dc2a864c4452895b5da76371f036cc4e7eac3`):

| id | category | text |
|---|---|---|
| `0affe10a` | other | User has created an aquascape |
| `cd88a414` | other | User has an RGB computer |
| `4189b276` | technical_fact | User has an RGB strip |
| `646250ec` | other | User is working on adding features and creating a 3D model for Luno |
| `30586215` | other | Vinn is working with Unity for 3D animation and coding |

Note: `4189b276` ("User has an RGB strip") classifies as `technical_fact`
purely because the substring `"ip"` (one of the classifier's keywords,
matching e.g. "IP address") happens to appear inside the word "str**ip**" -
a pre-existing quirk of the real, substring-based classifier, faithfully
reproduced here rather than smoothed over.

## 4. Validation before touching production

`recovery/validate_candidate_isolated.py` ran a full battery (schema
validity, JSON validity, unique ids, required-field validity, absence of
every fabricated field, timestamp validity, all 5 snapshot memories
present with exact text, AND live-loaded the candidate through the real
`luno.memory`/`luno.memory_context`/`luno.memory_retrieval`/
`luno.dashboard.collectors`/`memory_health_report()`/
`analyze_memory_maintenance()` code paths) entirely inside a fresh temp
directory, with every `luno.config.*_FILE` env var redirected there
BEFORE any `luno` module was imported. The real production file's hash
was confirmed byte-identical before and after that run.

## 5. Backup + atomic-write mechanism (Phase 6)

`luno/memory.py`'s existing `_save()`/`_load()` were extended IN PLACE -
no second persistence engine, no new storage location beyond a
`backups/` subfolder next to `long_term_memory.json`:

- **Before every write**, the CURRENT on-disk file (if any) is copied
  into `config/backups/long_term_memory.<YYYYMMDDTHHMMSSffffff>.json`
  (`_backup_current_memory_file()`).
- **The write itself is atomic**: new content is written to a `.tmp`
  file in the same directory, `fsync`'d, then swapped into place via
  `os.replace()` (`_atomic_write_json()`) - never a bare
  truncate-in-place. `os.replace()` was chosen specifically because it
  atomically overwrites an existing destination on both POSIX and
  Windows (unlike `os.rename()`, which raises on Windows if the
  destination exists). If anything fails before the final
  `os.replace()`, the original file is completely untouched and the
  throwaway `.tmp` file is best-effort cleaned up.
- **Retention**: at most `_MEMORY_BACKUP_RETENTION` (20) timestamped
  backups are kept, oldest pruned first, but never fewer than 1 -
  `_prune_memory_backups()`.
- **Load-time recovery**: if the primary file fails to parse,
  `_load()` now tries each backup newest-first
  (`_load_latest_valid_backup()`) before falling back to an empty
  store - "restart/reload loads the latest valid state" instead of
  silently losing everything to a single corrupted write.

## 6. Test-isolation guard (Phase 7)

**Audit finding**: `tests/conftest.py`'s existing autouse
`isolate_persistent_state` fixture already redirects
`config.LONG_TERM_MEMORY_FILE` (and every other writable persistent
state file) to a fresh `tmp_path`-derived location, and resets
`luno.memory._memories` to `[]`, before EVERY test collected under
`tests/`. Every test file that touches `LONG_TERM_MEMORY_FILE` was
checked (via a repo-wide grep) and all of them either rely on this
fixture or additionally `monkeypatch.setattr` their own specific
isolated path - none write to the real file. No standalone ad-hoc
helper script exists anywhere in the repository outside `tests/` either.

**Conclusion**: the incident was NOT caused by a gap in the existing
pytest test suite - it was caused by a bare, non-pytest script run with
no isolation at all, a failure mode the existing fixture was never
designed to catch (it only wraps pytest test collection). The fix
therefore has two parts:

1. A defense-in-depth guard added to `_save()`
   (`_refuse_if_pytest_targeting_unisolated_path()`): if
   `PYTEST_CURRENT_TEST` is set (pytest is running) AND the target path
   is not under the system temp directory (i.e. does not look like an
   isolated fixture path), `_save()` raises loudly instead of writing.
   Inert outside pytest - `PYTEST_CURRENT_TEST` is never set otherwise,
   so production runtime behavior is unaffected.
2. The backup/atomic-write mechanism above (Phase 6), which protects
   against this failure mode REGARDLESS of cause - any future accidental
   write (whether from pytest, a bare script, or a real bug) is now
   trivially recoverable from `config/backups/`, where before this
   sprint it was not recoverable at all.

No attempt was made to detect "is this an authorized production
process" in general (e.g. by inspecting `sys.argv`) - that would be
fragile and risks breaking legitimate runtime behavior (explicitly
forbidden). The going-forward discipline for ad-hoc verification is
process, not code: copy config to an isolated temp path (or use
pytest), never import `luno.memory` bare against the real file - now
documented in `ARCHITECTURE_GUARD.md`.

## 7. Restore (Phase 9)

`recovery/restore_to_production.py` performed the one authorized write
to production in this whole recovery: verified the current file's hash
still matched the audited damaged state, verified the candidate was the
already-validated 5-entry file, verified `config.LONG_TERM_MEMORY_FILE`
resolved to the real path, then called the real, hardened `memory._save()`.
That call automatically backed up the damaged 2-entry state to
`config/backups/long_term_memory.20260809T195155906907.json` (SHA256
`5832067433f8...`, matching the damaged state exactly) before atomically
writing the 5 recovered entries (new file SHA256
`e4a92097eb920e74b495b1bef05dc2a864c4452895b5da76371f036cc4e7eac3`).

## 8. Post-restore verification (Phase 10)

Reloaded via a fresh `luno.memory` import: 5 entries, ids match exactly,
text matches the snapshot exactly, zero fabricated metadata fields,
`schema_version=4` on all 5, dashboard/`retrieval`/`context assembly`/
`maintenance analysis` all succeed, `evaluate_memory()` reports zero
fabricated strengths and confidence bounded to only the deterministic,
evidence-free age-based staleness term (0.0 or 0.08 - never higher),
and every outcome-evidence counter (`get_memory_evidence_counts()`)
reads exactly 0 for all 5 entries. Production file hash confirmed
unchanged by the verification run itself.

## 9. What could NOT be recovered

- Any manual memory created, edited, or deleted between 2026-07-23 and
  2026-08-09.
- `importance`/`usefulness_score`/feedback counts/`evaluation_score`/
  retrieval history/conflict history for the 5 recovered memories, as
  they existed before the overwrite (these were never in the 2026-07-23
  snapshot to begin with - the snapshot predates the schema fields that
  would carry them).
- `updated_at` for the 5 recovered memories (the snapshot only ever
  had `created_at`).

## 10. Files created / modified

Created: `recovery/damaged_long_term_memory.json`,
`recovery/recovery_manifest.json`, `recovery/recovery_decision.md`,
`recovery/snapshot_2026-07-23.json`, `recovery/migrate_snapshot.py`,
`recovery/migrated_candidate.json`, `recovery/validate_candidate_isolated.py`,
`recovery/restore_to_production.py`, `tests/test_memory_persistence_hardening.py`,
`docs/change_impact/memory_recovery.md`, `config/backups/long_term_memory.20260809T195155906907.json`.

Modified: `luno/memory.py` (`_save()`/`_load()` hardened with backup +
atomic write + pytest guard; new `import shutil`/`import tempfile`),
`config/long_term_memory.json` (restored content).

Nothing was deleted.

## 11. Known limitations / remaining technical debt

- The 7/23-8/9 data gap is permanent and unrecoverable from any source
  available to this project.
- The backup/atomic-write mechanism protects `long_term_memory.json`
  only, per this sprint's own scope - the SAME class of risk still
  exists, unaddressed, for the other writer-capable persistent files
  (`relationship_state.json`, `episodic_memory.json`,
  `session_summaries.json`, `habit_memory.json`, `reminders.json`,
  `verified_facts.json`) - a natural, additive follow-up, not attempted
  here to keep this sprint's blast radius scoped to the file that was
  actually damaged.
- The pytest guard is a heuristic (path-under-system-temp-dir) - a test
  that deliberately points `LONG_TERM_MEMORY_FILE` at a real-looking
  path OUTSIDE pytest's own tmp_path (unusual, against every existing
  convention in this codebase) would not be caught by it; it is
  defense-in-depth, not a substitute for the isolation fixture.
- `luno/memory.py`'s Memory Decision Quality & Adaptive Retrieval sprint
  work remains PAUSED mid-implementation (Phase 4 of that sprint) - see
  `docs/change_impact/memory_recovery.md` §12/regression notes below;
  it will be resumed as its own, separate task per Vinn's own
  instruction not to mix it with this recovery.
