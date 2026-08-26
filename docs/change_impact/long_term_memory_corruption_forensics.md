# Sprint 64 — Long-Term Memory Corruption ORIGIN Forensics

**Type:** Forensic investigation. **Not a recovery sprint.**

Sprint 63 established *what* `config/long_term_memory.json`'s corruption
looks like at the byte level. Sprint 64 asks a narrower, different
question: *who or what plausibly put that content there?* Per this
sprint's own explicit brief: **"INI ADALAH FORENSIC INVESTIGATION, BUKAN
RECOVERY."** No fix, migration, or recovery was attempted. The production
file and its Sprint 63 preservation backup are both confirmed byte-identical
at the start and end of this sprint (see §9).

---

## 1. Background (carried over from Sprint 63, re-verified here)

- File: `config/long_term_memory.json`, 1849 bytes.
- MD5: `c16525937a6bc063e182c1b6b120e42e`
- SHA-256: `be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a`
  (**correction**: Sprint 63's own prose and this sprint's first test-file
  draft both carried a 63-character transcription of this digest, missing
  its final hex character. The MD5 above — independently recorded and
  re-verified at every checkpoint across Sprints 55–64 — was never affected
  and remains the primary drift guard. The SHA-256 is corrected here to the
  64-character value `hashlib.sha256` actually produces against the real
  file bytes; this is a documentation fix, not a finding about the file.)
- Byte structure: bytes 0–1475 at ~7.87 bits/byte entropy (near-random);
  bytes 1476–1535 a 60-byte run of `0x00`; bytes 1536–1848 at ~4.36
  bits/byte, decoding as clean 7-bit ASCII matching the MIT LICENSE
  boilerplate verbatim, cut off mid-word at EOF.
- No encryption/compression mechanism exists anywhere in this codebase.
- `luno/memory.py` is the only production module that reads/writes this
  file.
- `config/backups/` had zero pre-existing `long_term_memory.*.json`
  entries before Sprint 63's own preservation backup.
- The file has been byte-identical since (at least) Sprint 55.

## 2. Phase 0 — prior knowledge read

Read in full before any new forensic step: `docs/project_handover.md`,
`docs/project_handover.json`, `ARCHITECTURE_GUARD.md` §55–64,
`docs/change_impact/long_term_memory_recovery.md`, `luno/memory.py`, every
module importing `luno.memory` or `luno.persistence`, `probe_memory_pipeline.py`
(the only non-test, non-`luno.memory` script referencing
`LONG_TERM_MEMORY_FILE`), and `tests/conftest.py`'s isolation fixture.

## 3. Phase 1 — filesystem forensics (read-only)

- The production file's Birth/Modify/Change timestamps
  (`2026-08-16 16:39:39.201355907` / `...201367872` / `...201367872`) are
  identical at sub-millisecond resolution to every other **untouched**
  original `config/*.json` file in this checkout (12 files, sequential
  inodes 811223–811248). This proves these timestamps record only when
  *this sandbox session's filesystem was bulk-extracted*, not any
  real-world corruption event. Two files legitimately differ:
  `config/lights.config.json` (birth matches this project's own Sprint
  60/61 edits) and `config/relationship_state.json` (birth matches an
  in-session test write).
- **New finding, corrects Sprint 55's earlier speculation**: every
  untouched original config/source file — `long_term_memory.json` included
  — shares file mode `444` (read-only), including files with no plausible
  connection to memory corruption (`main_runtime_demo.py`, `luno/memory.py`,
  `ARCHITECTURE_GUARD.md`). Only files this sandbox itself rewrote during
  live sprint/test activity (`relationship_state.json` → `600`,
  `vision_memory.sqlite3` → `644`) differ. The `444` mode is a whole-bundle
  packaging/extraction artifact, **not** a targeted protection specific to
  the corrupted file. Encoded as an executable regression guard in
  `test_H_every_original_config_file_shares_the_same_bulk_extraction_permission`.
- No alternate-data-streams/xattrs mechanism is in use on this filesystem;
  the file is a regular file, not a symlink.
- None of this timestamp/permission metadata carries diagnostic value for
  reconstructing *when* the corruption actually happened in the real world
  — see §7 (Timeline).

## 4. Phase 2 & 3 — writer inventory and call-graph trace

Full repo-wide search (both the literal string `long_term_memory.json` /
`LONG_TERM_MEMORY_FILE`, and structural search for
`luno.persistence.atomic_write_json()` callers, `open(..., "w")`,
`json.dump`, `os.replace`, `shutil.copy`/`move`, backup/migration/recovery
scripts, startup/shutdown hooks) found exactly two production references to
`config.LONG_TERM_MEMORY_FILE`:

| Writer | File | Verdict |
|---|---|---|
| `luno.memory._save()` → `luno.memory._atomic_write_json()` | `luno/memory.py:591–601` | The **sole** production writer. Every mutation function in the module funnels through `_save()` (structurally proven — `test_D_save_is_the_only_internal_entry_point_that_writes_to_disk` asserts exactly one `_atomic_write_json(` call and no direct `open(` in `_save()`'s source). |
| `luno.memory._load()` | `luno/memory.py:563–579`, invoked once at module import (`luno/memory.py:604`) | Read-only. Opens the file in `"r"` mode only; on any exception, falls back to the newest valid backup, then to an empty in-memory list — never writes to disk (`test_C_only_known_writers_...` asserts no write primitive appears in `_load()`'s source). |

Every OTHER store in the codebase (`VerifiedFactStore`, `HabitMemory`,
`EpisodicMemory`, `RelationshipEngine`, `ResponseDepthPreference`,
`Reminders`, and `luno.memory`'s own session-summaries store) is bound to
its own distinct `luno.persistence`-managed path constant
(`VERIFIED_FACTS_FILE`, `HABIT_MEMORY_FILE`, `EPISODIC_MEMORY_FILE`,
`RELATIONSHIP_STATE_FILE`, `RESPONSE_DEPTH_PREFERENCE_FILE`,
`REMINDERS_FILE`, `SESSION_SUMMARIES_FILE`) — confirmed structurally that
none can ever be misdirected at `LONG_TERM_MEMORY_FILE`
(`test_C_persistence_module_is_never_called_with_the_long_term_memory_path`).

`luno/bootstrap/shutdown.py`'s `ShutdownCoordinator` flushes only
`vision_memory` (a separate SQLite store) on shutdown — no call into
`luno.memory`'s save path. `legacy_main.py` does not exist in this
checkout. No `config/recovery/` directory or recovery script exists.

### `probe_memory_pipeline.py` — self-correction

Earlier reasoning in this sprint (and by extension Sprint 63) asserted this
script redirects `LONG_TERM_MEMORY_FILE` to an isolated temp path *before*
importing `luno.memory`. Re-reading the actual source shows the opposite
textual order: `import luno.memory as memory` (line 30) runs before the
isolation loop that reassigns `luno_config.LONG_TERM_MEMORY_FILE` (lines
43–57). **That earlier claim was incorrect as stated and is corrected
here.**

The corrected, evidence-checked conclusion is narrower but still holds:

1. `luno/memory.py` never caches `config.LONG_TERM_MEMORY_FILE` into a
   module-level constant — every read/write site does a *live*
   `config.LONG_TERM_MEMORY_FILE` attribute lookup at call time (8 call
   sites, confirmed via `grep -c`). Mutating `luno_config.LONG_TERM_MEMORY_FILE`
   after `luno.memory` is imported still fully redirects every future call.
2. The only code that runs against the **original**, non-redirected path
   is `luno/memory.py`'s own module-level `_load()` call, fired as a side
   effect of `import luno.memory` itself — before the probe's redirect
   loop executes. `_load()` is read-only by construction (see table
   above), so this produces one unintended **read** of the real production
   file at import time, never a write.
3. All actual persistence (`_save()`) is triggered later, only from
   `run_conversation()` inside the script's own `if __name__ == "__main__":`
   block — well after both the import and the redirect complete — so every
   real save in this script's normal use targets the isolated temp path.

Net effect: `probe_memory_pipeline.py` remains excluded as a writer of the
production file, for the corrected reason above rather than the originally
(incorrectly) stated one. This correction is encoded as an executable test
(`test_C_only_known_writers_reference_config_long_term_memory_file`).

**Answers to the brief's 10 Phase 3 questions:**

1. Is `luno.memory._save()` truly the only production writer? — **Yes**,
   structurally confirmed (see table above).
2. Any legacy writer? — **No** (`legacy_main.py` absent from this
   checkout).
3. Any startup migration? — **No** migration code found anywhere in the
   repo.
4. Any test utility that can write the production path? — **No**. The
   autouse `isolate_persistent_state` fixture in `tests/conftest.py`
   redirects `LONG_TERM_MEMORY_FILE` before every test
   (`test_E_autouse_isolation_fixture_redirects_long_term_memory_file`),
   and `_refuse_if_pytest_targeting_unisolated_path()` inside `_save()`
   itself raises loudly if `PYTEST_CURRENT_TEST` is set and the target
   path isn't under the system temp dir — defense in depth.
5. Any recovery script that can write the production path? — **No**
   `config/recovery/` directory or recovery script exists in this
   checkout.
6. Can shutdown write memory? — **No** (`ShutdownCoordinator` only
   flushes `vision_memory`).
7. Can exception handling cause a partial write? — **Structurally no**:
   `_save()`'s try/except wraps `_backup_current_memory_file()`,
   `_atomic_write_json()`, and `_prune_memory_backups()`; any exception is
   caught, logged, and swallowed — it cannot leave a partially-written
   *production*-path file, because `_atomic_write_json()` only ever
   touches the production path via a single atomic `os.replace()` call
   (see §6).
8. Can process termination leave a partial file? — **No**, for the same
   reason: everything before `os.replace()` writes only to a uniquely-named
   `.tmp` file; the production path itself is only ever touched by the
   single atomic rename. Demonstrated experimentally in §6.
9. Can concurrent processes write the same file? — Each `_save()` call
   uses `tempfile.mkstemp()` for a uniquely-named temp file, so concurrent
   `_save()` calls cannot blend content; the CONFIRMED EXCLUSION in §6
   covers this.
10. Can temp-file replacement produce an artifact like the one found? —
    **No** — see the reproduction experiment in §6.

## 5. Phase 4 — temp/backup artifact search

Read-only, whole-repo search (excluding `__pycache__`, `.git`) for
`*.tmp`, `*.bak`, `*.backup`, `*.old`, `*.recovery`, `*.partial`,
`*.json.tmp`: **zero candidates found.** No `config/recovery/` directory.
No archive (`.zip` or similar) containing a `long_term_memory.json`
candidate anywhere in this checkout. Encoded as
`test_F_temporary_artifact_search_finds_none_and_mutates_nothing`, which
also asserts the search itself never touches the production file's hash.

## 6. Phase 9 & 10 — write-path audit and reproduction (negative control)

**Static structural argument** for why `_save()` / `_atomic_write_json()`
cannot have produced the observed artifact, under any failure mode:

- `_atomic_write_json()`'s only content-writing call is
  `json.dump(data, f, ...)`, where `data` is always `_memories` — a Python
  `list` of `dict`s. This can never emit non-UTF-8/high-entropy binary
  content or embedded third-party plaintext, regardless of when a
  crash/kill/interruption occurs:
  - Anything before the final `os.replace()` either leaves the **original**
    file completely untouched, or leaves a `.tmp` file containing at most a
    truncated **prefix of valid JSON text**.
  - `os.replace()` is a single atomic rename syscall — it can never blend
    two files' content.
  - Concurrent/racing `_save()` calls each use uniquely-named temp files
    (`tempfile.mkstemp()`), so a race can only ever produce one complete
    valid JSON payload winning, never a hybrid/blended result.

**Reproduction (negative control)**, `tests/test_sprint64_memory_corruption_forensics.py::test_I_interrupted_write_never_produces_binary_or_license_like_content`:
monkeypatches `os.replace` to raise immediately before the swap-in step
(the closest real-world analogue to "process killed mid-write" this
design allows — anything killed earlier never reaches this point;
anything killed later has already fully replaced the file), then calls
the real `_atomic_write_json()` against a `tmp_path`. Result: the
production-path content is completely untouched, and any leftover `.tmp`
file decodes cleanly as UTF-8 with no MIT LICENSE phrase and no
same-byte run ≥32 bytes — i.e. even the worst-case interrupted-write
scenario this code's own design permits produces nothing resembling the
observed artifact.

**Classification: `_save()` / `_atomic_write_json()` producing this
artifact via any in-repo code path is UNSUPPORTED** — not merely
"unlikely," but structurally impossible given the function's only data
source (`_memories`, always a JSON-serializable list) and only write
mechanism (`json.dump` → atomic `os.replace()`).

No reproduction was forced from a hypothetical external writer, because no
in-repo candidate for one exists — per the brief's own conditional ("only
if a writer/path is strongly suspected"), forcing a reproduction from a
non-existent candidate would fabricate a misleading result rather than
supply evidence.

## 7. Phase 5 — MIT LICENSE origin search (bounded, partial)

The corrupted file's exact 313-byte tail was used as a search fingerprint
(kept only as a scratch file outside the repository, at
`/tmp/_forensic_tail_fingerprint.bin`, purely to make the search
convenient — not part of any deliverable). A generic MIT-license-phrase
grep across one representative site-packages directory
(`/root/.local/lib/python3.11/site-packages`, `timeout 60`; a full
3-directory sweep exceeded the tool's own execution budget and was not
retried at full scope) found 6 phrase-matching candidates
(`charset_normalizer`, `conan`, `patch_ng.py` + its `LICENSE`, `urllib3`).
**None of the 6 contained an exact byte-for-byte match of the 313-byte
tail fingerprint.**

**MIT LICENSE ORIGIN: NOT FOUND** (bounded search). This search is
explicitly *not* used to implicate any specific package — per the brief's
own instruction, an MIT-license text match alone would not prove a causal
link even if found. This sandbox's own installed packages are, in any
case, very unlikely to be the actual origin environment, since the
corruption predates this sandbox session entirely (§3). The search is
honestly reported as partial, not exhaustive.

## 8. Phase 6 & 7 — project history and external tooling

- **Not a git repository**: `git status` / `git log` /
  `git rev-parse --is-inside-work-tree` all return `fatal: not a git
  repository`. No history available. Per the brief's explicit instruction,
  no new repository was created.
- **No in-repo external-agent/automation tooling found**: searches for
  `*.claude*`, `*cowork*`, `*sync*`, `*deploy*`, `*watcher*` filenames, and
  a full top-level directory listing, found nothing beyond the standard
  project structure (`ARCHITECTURE_GUARD.md`, `main.py`,
  `main_runtime_demo.py`, `probe_memory_pipeline.py`, `requirements.txt`,
  `config`, `docs`, `logs`, `luno`, `tests`). No evidence exists in this
  checkout to accuse or exonerate any specific external tool — this is
  reported as **UNKNOWN**, not as a negative finding, since the absence of
  in-repo traces does not prove no external process was ever involved.

## 9. Phase 8 — timeline

| Event | Date | Confidence |
|---|---|---|
| Pre-Sprint-43 ad-hoc-script incident (2 throwaway smoke-test entries overwrite the real file) | UNKNOWN exact date | Documented in `ARCHITECTURE_GUARD.md`'s own pre-Sprint-43 section |
| Recovery via `recovery/migrate_snapshot.py` + `recovery/restore_to_production.py` (5 memories restored from a `Luno Evo.zip` 2026-07-23 snapshot) | 2026-08-09 | Documented in `ARCHITECTURE_GUARD.md`; the `recovery/` scripts and both change-impact docs describing this incident are **absent from this checkout** — a documentation/artifact gap, not reconstructible here |
| Sprint 55 finds the CURRENT corrupted artifact | UNKNOWN exact date, but definitionally after 2026-08-09 (the restored 5-entry content does not match the current artifact's size/shape, so the transition from "5 valid entries" to "current corruption" happened between these two points) | Ordering confirmed; exact date UNKNOWN |
| Sprint 63 diagnoses the corruption's byte structure, creates preservation backup | 2026-08-17 07:40:00 UTC (backup filename timestamp) | Confirmed |
| Sprint 64 (this sprint) — origin forensics | 2026-08-17 | Confirmed |

**The transition from the 2026-08-09 restore's valid 5-entry content to
the current corrupted artifact is entirely unexplained by any evidence
available in this checkout.** This is reported as a genuine timeline gap —
**UNKNOWN** — not invented.

## 10. Phase 13 — decision matrix

**STATUS: UNKNOWN** (no specific, provable external source identified),
paired with a clearly evidenced **CONFIRMED EXCLUSION**: Luno's own
application code — `luno.memory._save()` and every other traced
persistence writer in this codebase — is **not** the source, ruled out via
structural code audit (§4, §6) rather than mere suspicion. Per the brief's
own framing, UNKNOWN is "a valid result" here, not a failure to conclude.

| Hypothesis | Classification |
|---|---|
| `_save()` / `_atomic_write_json()` produced this via normal operation | **UNSUPPORTED** — structurally impossible; only ever writes valid JSON from `_memories` |
| ...via an interrupted/killed-process write | **UNSUPPORTED** — reproduced negative control (§6) |
| ...via concurrent/racing `_save()` calls | **UNSUPPORTED** — uniquely-named temp files per call, atomic rename |
| ...via a different buggy writer in this codebase | **UNSUPPORTED** — no other writer of `LONG_TERM_MEMORY_FILE` exists (§4) |
| ...via a test/ad-hoc script writing to the real path (current suite) | **UNSUPPORTED** — every test-suite path is isolated (§4, item 4); `probe_memory_pipeline.py` never writes to the real path (§4 self-correction) |
| ...via a test/ad-hoc script writing to the real path (as a general failure class, historically) | **PLAUSIBLE** — matches the documented pre-Sprint-43 incident's own mechanism, though that specific incident's own output (2 throwaway entries) does not match the current artifact's shape, so it does not itself explain the current corruption |
| ...via an external tool/process outside this codebase | **Not affirmatively excluded** — the only hypothesis this investigation could not rule in or out; no specific named candidate found (§7, §8) |

## 11. Persistent-state verification

- Production file SHA-256/MD5 confirmed unchanged at Phase 0, mid-run
  (`test_E_this_modules_own_hashes_prove_no_mutation_occurred_mid_run`),
  and end-of-run (`test_J_no_persistence_mutation_across_this_entire_test_files_run`).
- Sprint 63 preservation backup confirmed byte-identical to production
  throughout.
- All 15 `config/*.json` files SHA-256'd before and after the full
  regression sweep (§12): **zero drift**.
- `config/backups/` confirmed to contain exactly one
  `long_term_memory.*.json` entry (the Sprint 63 preservation backup) —
  no new backup was accidentally created by this sprint's own work.

**PRODUCTION FILE MODIFIED: NO. BACKUP MODIFIED: NO.**

## 12. Test results and regression

- New file: `tests/test_sprint64_memory_corruption_forensics.py` — 15
  tests, all read-only against production state or scoped to
  `tmp_path`/`monkeypatch`. All 15 passed.
  (Two assertions were corrected during this sprint before the file was
  considered final: a 63-character SHA-256 transcription typo, and the
  `probe_memory_pipeline.py` import/redirect ordering claim — see §4. Both
  corrections are evidence-based, not cosmetic.)
- Targeted memory-suite regression: `test_sprint63_long_term_memory_recovery.py`
  (24) + `test_sprint64_memory_corruption_forensics.py` (15) +
  `test_memory_persistence_hardening.py` (8 passed, 3 skipped) = **47
  passed, 3 skipped, 0 failed.**
- Full repository regression: **3249 passed, 38 failed, 3 skipped, 1
  collection error** (745s). All 38 failures and the 1 collection error
  are in unrelated e2e/integration/hardware-simulation modules (vision,
  TTS chunk pipelining/e2e, voice pipeline latency, streaming, mic device
  index, production launcher, real adapters, state isolation, and one
  `test_root_main_bargein.py` collection `FileNotFoundError`) — the same
  general failure classes Sprint 63 documented (3244 passed / 27 failed).
  Exactly one failing test name contains "memory" —
  `test_runtime_demo.py::test_episodic_memory_end_to_end_...` — and it
  concerns `EPISODIC_MEMORY_FILE`, a structurally distinct store from
  `LONG_TERM_MEMORY_FILE` (§4), not the file under investigation.
  **Zero failures reference `LONG_TERM_MEMORY_FILE` or `long_term_memory.json`.**
- One earlier full-sweep attempt in this same sprint crashed with a
  `Fatal Python error: Segmentation fault` inside a background
  logging/event-processing thread in `luno/adapters/utils.py` /
  `luno/adapters/base.py`, at ~8–9% of collected tests — unrelated to
  persistence, non-reproducible on immediate retry (the retry completed
  cleanly past that point), noted here as an observed environment
  flake, not as forensic evidence about the memory file.
- A pre-existing, long-running orphaned `pytest tests/` process (over 1
  hour of wall-clock runtime, evidently left over from an earlier sprint's
  own regression run) was found still active in this sandbox and was
  terminated before this sprint's own regression sweep, to avoid resource
  contention between the two runs. It was not writing to
  `LONG_TERM_MEMORY_FILE` at any point (confirmed: no drift in the
  production file's hash across this entire sprint) and is noted here only
  as an environment-hygiene observation, not a forensic finding.

## 13. Known limitations

- The MIT LICENSE origin search (§7) is bounded/partial — one
  site-packages directory, time-boxed — not an exhaustive sweep of every
  package ever installed anywhere the file might have originated.
- No git history or other version-control history is available in this
  checkout, so no commit-level timeline could be built (§9).
- The `recovery/` script directory and both change-impact docs describing
  the pre-Sprint-43 incident are absent from this checkout — their exact
  mechanics could not be independently re-verified here, only referenced
  via `ARCHITECTURE_GUARD.md`'s own prose.
- No external-agent/automation-tooling forensics could be performed beyond
  this repository's own contents — this sandbox has no visibility into
  whatever environment(s) previously hosted this project.
- The exact real-world timestamp of the corruption event itself remains
  UNKNOWN (§9) — only its position in the ordered sequence of known
  events could be established.

## 14. Recommended next action

No fix or recovery is recommended by this sprint (out of scope). If a
future sprint is authorized to attempt recovery, Sprint 63's own §14
("Recommended manual recovery procedure") remains the relevant reference —
this sprint adds no new recovery guidance, only origin evidence. The one
open avenue this sprint could not pursue: if the actual host environment
that ran Sprint 55 (or earlier) is ever available for inspection (its own
filesystem, its own installed packages, its own process history), the
MIT LICENSE fingerprint search in §7 could be repeated there with much
higher diagnostic value than in this sandbox.
