# Sprint 63 — Long-Term Memory Persistence Recovery & Integrity Investigation

**STATUS: DIAGNOSIS ONLY. `config/long_term_memory.json` was NOT modified,
migrated, or recovered. STOP CONDITION applies.**

## 1. Objective

Investigate `config/long_term_memory.json`'s pre-existing, repeatedly
flagged (Sprint 55/56/57, re-confirmed byte-identical every sweep since)
failure to load, and fix it if — and only if — it can be done safely,
with high confidence, and without any risk of fabricating or losing
memory data.

## 2. Phase 0 — what was already known before this sprint

Sprint 55 first flagged the failure (`'utf-8' codec can't decode byte
0x9c in position 4: invalid start byte`). Sprint 57 performed a dedicated
forensic pass: not valid JSON, not gzip, not standard zlib, not any
common text encoding (UTF-8/UTF-16 fail; Latin-1 "succeeds" into
mojibake), whole-file Shannon entropy 7.65 bits/byte (of max 8.0), no
backup exists for this file specifically. Conclusion: format/root cause
UNKNOWN, explicitly deferred.

Separately, an EARLIER, unrelated incident and recovery is documented in
`ARCHITECTURE_GUARD.md`'s "Memory Recovery & Persistence Hardening"
section (predates this project's Sprint 43+ numbering): an ad-hoc script
once overwrote the real file with 2 throwaway smoke-test entries; a
2026-07-23 snapshot (bundled in a `Luno Evo.zip` the user had) was
migrated (5 memories, via `recovery/migrate_snapshot.py`) and restored
to production on 2026-08-09 via `recovery/restore_to_production.py`.
That same sprint also added the backup/atomic-write hardening layer this
investigation relies on (`luno/memory.py`'s `_backup_current_memory_
file()`/`_atomic_write_json()`/`_load_latest_valid_backup()`/
`_prune_memory_backups()`).

**Important finding of this sprint's own Phase 0:** `docs/change_impact/
memory_recovery.md` and `docs/change_impact/persistent_state_hardening_
v2.md` — both referenced by `ARCHITECTURE_GUARD.md` — do **not** exist in
this checkout, nor does the `recovery/` directory (`migrate_snapshot.py`/
`restore_to_production.py`) they describe. Per this project's own
"source code is authority over docs" convention, this sprint treats the
CODE that survived (`luno/memory.py`'s hardening functions, `tests/
test_memory_persistence_hardening.py`, `tests/test_persistent_state_
hardening.py`, `luno/persistence.py`) as ground truth, and notes the
missing docs/scripts as a documentation/artifact gap, not something this
sprint can reconstruct or verify further.

**The restored 5-memory content from 2026-08-09 is NOT what's on disk
today.** The restore would have produced small, valid JSON (5 entries).
The current file is 1849 bytes of the exact byte-for-byte content Sprint
55 already found broken. Something replaced the file's content again,
at an unknown point, between the 2026-08-09 restore and Sprint 55's
first observation of it — this sprint could not determine when or how
(see §7).

## 3. Phase 1 — forensic analysis (this sprint's own findings)

**A/B — writer/reader.** `luno/memory.py` is the ONLY module in this
repository that reads or writes `config.LONG_TERM_MEMORY_FILE` (verified
by a repo-wide grep for both the constant name and the literal
`long_term_memory.json` string — every other reference is a comment,
docstring, or an unrelated module's own file, e.g. `long_term_memory`
used as a *source name string* inside `luno/memory_retrieval/`, a
different concept entirely). `luno/config.py` defines the path:
`LONG_TERM_MEMORY_FILE = os.getenv("LONG_TERM_MEMORY_FILE",
os.path.join(DATA_DIR, "long_term_memory.json"))` — not overridden in
this checkout's `.env`. `probe_memory_pipeline.py` (a leftover
investigation script from an earlier sprint) imports the constant name
but redirects it to an isolated temp path BEFORE ever importing
`luno.memory` — it has never written to the real file.

**C — layer analysis (new this sprint, more precise than Sprint 57's own
whole-file-only entropy measurement).** The file is NOT a single
uniform layer:

| Region | Offsets | Size | Shannon entropy | Character |
|---|---|---|---|---|
| Head | 0–1475 | 1476 bytes | **7.87** bits/byte | Near-random / high-entropy binary |
| NUL run | 1476–1535 | 60 bytes | 0.0 (constant) | Literal `0x00` padding |
| Tail | 1536–1848 | 313 bytes | **4.36** bits/byte | Readable ASCII English text |

The tail decodes cleanly as 7-bit ASCII and reads, verbatim: *"...ert.
\n\nPermission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation files
(the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge,
publish, distribute, sublicense, and/or sell c"* (cut off mid-word at
EOF) — this is the **standard MIT LICENSE boilerplate**, used verbatim
by thousands of open-source projects.

**Why this matters:** a genuine single-layer encrypted or compressed
blob has uniform entropy throughout its length and essentially never
produces a 60-byte run of one identical byte value (probability
~(1/256)^59 in truly random ciphertext — not just unlikely, for all
practical purposes impossible) nor a clean, grammatically correct
plaintext sentence at an interior offset. NUL-byte padding runs and
embedded third-party license text ARE, however, completely ordinary in
compiled binaries, bundled resources, and similar artifacts (many
open-source-dependent binaries embed their dependencies' license text
near the end of the file for compliance). This is much stronger,
specific evidence than "entropy is high" alone: **this file's current
content is very unlikely to be genuine encrypted/compressed
serialization of Luno memory data at all** — it looks instead like an
accidental fragment of some unrelated binary file.

**D — encryption keys.** Repo-wide search found no encryption/
decryption key, cipher usage, key-derivation function, salt, IV, or
secret-management code anywhere touching `LONG_TERM_MEMORY_FILE` or
`luno/memory.py`. This project's memory persistence has never used
encryption at any point in its history (every other memory-adjacent
sprint's own docs describe plain JSON throughout). This further supports
"this isn't Luno-encrypted data" — there's no encryption mechanism in
this codebase that could have produced it in the first place.

**E/F — file vs. loader.** The loader (`luno.memory._load()`) uses plain
`json.load()`, exactly as documented, and is proven (§5, Phase 2) to
behave correctly and gracefully for every tested failure mode. This is
not a loader bug. The file's content, independently verified byte-for-
byte in this sprint (§3.C), is not valid JSON, not a recognized
compression format, and (per the new entropy-discontinuity finding) not
even internally consistent with being a single homogeneous
encrypted/compressed payload. The damage is in the FILE, not the code
that reads it.

**G — other copies.** `config/backups/` exists but contains ONLY
`relationship_state.*.json` entries (11, all from this checkout's own
recent test/sprint activity) — zero pre-existing `long_term_memory.*.
json` backups. This directly PROVES the current corruption did not
happen through `luno.memory._save()`'s own normal write path, since that
path unconditionally calls `_backup_current_memory_file()` first — if a
normal save had ever run against this corrupted state (or produced it),
a backup of whatever preceded it would exist. No `Luno Evo.zip`, no
`recovery/` directory, and no other historical snapshot are present
anywhere in this sandbox's filesystem.

**H — migration code.** The earlier "Memory Recovery & Persistence
Hardening" sprint's `recovery/migrate_snapshot.py`/`restore_to_
production.py` did once transform a 2026-07-23 snapshot into the
current schema and write it to production (2026-08-09) — but that
predates the CURRENT corruption (§2) and is not present in this checkout
to re-run or verify further. No other migration code touches this file.

**I — does the app still work without it?** Yes, directly re-confirmed
in this sprint (and every prior sprint's own console output):
`luno.memory._load()` catches the decode failure, tries
`_load_latest_valid_backup()` (finds none), logs `"No valid backup
available either - starting from an empty long-term memory store"`, and
the rest of the application runs completely normally from there —
proven, not assumed, via `test_A_current_failure_reproduces_against_a_
copy` and the wider full-repository regression sweep (§8), which
depends on `luno.memory` importing successfully at collection time
across ~3200 other tests.

## 4. Phase 2 — reproduction (all against copies, never the production file)

`tests/test_sprint63_long_term_memory_recovery.py` — every scenario
listed in the brief's own Phase 2 checklist:

1. Current file (byte-for-byte copy) → current loader → exact same
   graceful failure as production (`test_A`).
2. Valid JSON → loader → correct load (`test_B`).
3. Missing file → graceful empty-store fallback, and the loader never
   creates the file as a side effect (`test_F`).
4. Malformed JSON → graceful fallback (`test_C`).
5. Empty file → graceful fallback (`test_E`).
6. Truncated file (valid JSON cut mid-way) → graceful fallback (`test_D`).
7. Existing valid memory survives a save→backup→reload round trip with
   every field intact (`test_J`).
8. A failed load NEVER silently replaces existing persistent data — the
   primary file's bytes and even its mtime are provably untouched by a
   failed `_load()` call (`test_K`).

Plus a synthetic high-entropy binary payload (unrelated to the real
file's exact bytes) to prove the graceful-fallback behavior is general,
not a special case for this one file (`test_G`).

## 5. Phase 3 — safe fix determination

Per the brief's own explicit decision tree:

- **Loader bug + valid file format?** No — the file is not valid under
  any format this sprint could identify (§3.C/E), so this branch does
  not apply.
- **Writer bug?** No — `_save()`'s own write path is proven correct by
  the pre-existing, still-passing `tests/test_memory_persistence_
  hardening.py` (11 scenarios) and by this sprint's own `test_H`/`test_
  I_*`/`test_J`. No writer bug produced this state (§3.G's own backup-
  absence proof shows the corruption bypassed `_save()` entirely).
- **File corrupted but recovery source available?** No — no backup, no
  snapshot, no `recovery/` script, and no external source reachable from
  this sandbox exists for the CURRENT state (§3.G/H).
- **File encrypted/custom format, key/format not provable?** **Yes —
  this is the applicable branch.** Per the brief: *"JANGAN mencoba
  brute-force atau menebak format. STOP CONDITION berlaku. Dokumentasikan
  bahwa recovery tidak aman dilakukan otomatis."*

**STOP CONDITION applied.** No attempt was made to decode, decrypt, or
reconstruct memory content from this file's bytes. Beyond the format
being unprovable, this sprint's own new evidence (§3.C) makes a stronger
claim: the content likely isn't derived from memory data in the first
place, so there is no "original memory" to recover FROM these specific
bytes even in principle — any content-based "recovery" attempt would
mean fabricating memory data, which every rule this sprint operates
under (and this project's standing invariants) explicitly forbids.

## 6. Phase 4/5 — atomic persistence safety / memory integrity

No writer or recovery-path code change was made (none was warranted —
§5). The existing atomic-write contract (`luno/memory.py`'s
`_atomic_write_json()`: temp file → write → flush → `fsync` → validate
via successful `json.dump` → `os.replace()`) was inspected, re-verified
still correct via `test_I_atomic_write_leaves_original_untouched_on_
failure`/`test_I_atomic_write_success_replaces_content_fully`, and left
completely unchanged. Per Phase 5's own explicit decision requirement
("recover all / recover none / recover safely identifiable subset"):
this sprint's explicit decision is **recover none** — no field, ID,
timestamp, importance value, or fact was extracted from the corrupted
file, because none could be identified with any confidence as genuine
memory content in the first place.

## 7. What this sprint could NOT determine

- The exact identity of the binary artifact whose fragment appears to
  make up this file's content (no matching source file exists anywhere
  in this sandbox to compare against).
- The exact date/mechanism by which the file entered this state (no
  filesystem metadata survives file transfer into this sandbox; every
  full regression sweep since Sprint 55 has found it byte-identical, so
  it predates this tracked project history and this sandbox has no way
  to look further back).
- Whether a LATER, valid backup/export of the user's real long-term
  memory (postdating 2026-08-09's restore, predating whatever produced
  the current state) exists anywhere on the user's own machine or
  cloud storage — this sandbox has no access to check.

## 8. Test results

`tests/test_sprint63_long_term_memory_recovery.py` — **24 passed, 0
failed** (8 forensic regression-guard tests + scenarios A–O per the
brief's own Phase 6 checklist, some combined/split for clarity — G
covers "corrupted payload" with a synthetic fixture; H/I/J/K/L/M/N/O map
directly to the brief's own lettering).

## 9. Targeted regression

Every memory-related test file in this repository (28 files: `test_
episodic_memory.py`, `test_manual_memory.py`, `test_memory_adaptive_
retrieval.py`, `test_memory_comparison_topic_preservation.py`, `test_
memory_confidence.py`, `test_memory_conflict.py`, `test_memory_conflict_
resolution.py`, `test_memory_context.py`, `test_memory_continuity.py`,
`test_memory_dashboard.py`, `test_memory_decision_quality.py`, `test_
memory_evaluation.py`, `test_memory_guard.py`, `test_memory_
intelligence.py`, `test_memory_learning.py`, `test_memory_maintenance.
py`, `test_memory_outcome_telemetry.py`, `test_memory_persistence_
hardening.py`, `test_memory_prompt_injection.py`, `test_memory_prompt_
intelligence.py`, `test_memory_regression.py`, `test_memory_retrieval.
py`, `test_memory_retrieval_decision_quality_reaudit.py`, `test_memory_
session_summary_api_compatibility.py`, `test_memory_topic_retention.py`,
`test_memory_voice_observability.py`, `test_temporal_memory_timeline_
awareness.py`, `test_persistent_state_hardening.py`) plus this sprint's
own new file — **1103 passed, 3 skipped, 0 failed**.

## 10. Full repository regression

See `docs/testing/regression_baseline.md`'s own Sprint 63 section for
the exact numbers — run via the same established methodology (`pytest
tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/test_root_
main_bargein.py --deselect tests/test_dashboard.py::test_36_audio_
capture_store_unit_behavior --timeout=60 --timeout-method=signal`),
with every failure individually classified against the pre-existing
environment/infrastructure and full-suite-only timing-interference
classes documented since Sprint 55.

## 11. Persistent-state verification

`config/*.json` (15 files) MD5-hashed before and after both the targeted
memory-suite run and the full repository sweep — **byte-identical**,
INCLUDING `config/long_term_memory.json` itself
(`c16525937a6bc063e182c1b6b120e42e`, matching every prior sprint's own
finding since Sprint 55). The ONLY filesystem change this sprint made
anywhere in `config/` is a single NEW, additive file: `config/backups/
long_term_memory.<timestamp>.pre_sprint63_forensic.json` — a byte-
identical (SHA-256-verified), read-only (`chmod 444`) preservation copy
of the corrupted file's current bytes, created via a direct file copy
(not through `luno.memory`'s own save path, since the corrupted content
was never validated/loaded application data to begin with). The
production file itself was never opened for writing, never truncated,
never had its permission bit changed (still `-r--r--r--`/`444`).

## 12. Known limitations

- The exact root cause (what specific process/tool wrote this content to
  this path) remains unidentified — see §7.
- Long-term memory content between (at latest) 2026-08-09's restore and
  whatever event produced the current state remains unrecovered by this
  sprint, exactly as it was before this sprint began.
- No live verification against a real, running Luno instance with a
  fresh/interactive memory-save cycle was performed (out of this
  sprint's own scope, and orthogonal to the diagnosis — the existing
  hardening test suite already proves the save/backup/atomic-write path
  independently).

## 13. Was the production file migrated?

**No.** `config/long_term_memory.json` is byte-for-byte identical to its
state at the start of this sprint (and identical to every prior sprint's
own recorded hash since Sprint 55). No migration, recovery, or rewrite
was performed. The only new artifact is the additive, read-only
preservation backup described in §11.

## 14. Recommended manual recovery procedure

Since nothing in this sandbox can identify or reverse whatever produced
the current file content, the only paths to recovering the user's real
long-term memory data are OUTSIDE this sandbox, on the user's own
systems:

1. Check the original `E:\Luno Evo` device (or wherever this project's
   checkout is normally hosted) for any snapshot, export, sync-history
   version, or editor-local-history copy of `long_term_memory.json`
   dated AFTER 2026-08-09 (the last known-good restore) and BEFORE
   whatever produced the current corrupted state — Windows File History,
   OneDrive version history, a git history if this path was ever
   tracked, or a manual backup the user made.
2. If such a copy is found, it should be validated (parses as JSON, is a
   list of memory-shaped objects, schema-consistent with `luno/memory.
   py`'s current `schema_version`) BEFORE being considered for
   restoration — the same validate-before-replace discipline this
   sprint's own reproduction tests exercise.
3. If no such copy exists, the long-term memory store should be treated
   as starting fresh from empty (its current, safe, already-operating
   state) — consistent with how the application already behaves today.

## 15. STOP CONDITIONS — evaluated

Per the brief's own list: "encryption format tidak dapat diidentifikasi"
— **triggered** (§3.C/D — no recognized format, and new evidence
suggests it isn't even genuine ciphertext); "key/secret yang diperlukan
tidak tersedia" — **triggered** (§3.D — no key/encryption mechanism
exists in this codebase at all); "file memiliki format custom yang tidak
dapat diverifikasi" — **triggered**; "recovery berpotensi kehilangan
memory" — not directly applicable (no recovery was attempted); "hanya
ada satu copy file dan recovery tidak dapat divalidasi" — **triggered**
(§3.G — zero usable backups, no external source); "tidak dapat
dibuktikan bahwa hasil recovery equivalent terhadap data asli" —
**triggered** (moot, since no recovery was attempted, but would apply if
one had been). Given multiple STOP CONDITIONS independently apply, this
sprint's diagnosis-only outcome is the only responsible one.

## 16. Next recommended sprint

Not a code sprint — a manual, out-of-band action by the user (§14).
Separately, if the user locates a valid, dated `long_term_memory.json`
snapshot, a small, dedicated "verified restoration" sprint could apply
the SAME validate-then-atomic-replace discipline this sprint's own
tests already prove works (`_atomic_write_json()`, pre-write backup)
to install it safely. No code changes are needed to support that when
the day comes — the existing hardening layer already handles it.
