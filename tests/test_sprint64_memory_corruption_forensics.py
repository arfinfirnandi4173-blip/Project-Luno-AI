"""
tests/test_sprint64_memory_corruption_forensics.py
======================================================

Sprint 64 - Long-Term Memory Corruption ORIGIN Forensics.

**This is a forensic investigation, not a recovery sprint.** Sprint 63
diagnosed WHAT `config/long_term_memory.json`'s corruption looks like
(entropy discontinuity, NUL run, embedded MIT LICENSE tail) and
concluded no safe automated recovery was possible. Sprint 64 asks a
narrower, different question: WHO or WHAT plausibly put that content
there? See `docs/change_impact/long_term_memory_corruption_forensics.md`
for the full writeup this file's tests support.

Every test in this file is READ-ONLY against production state, or
operates entirely inside a throwaway `tmp_path`. Nothing here ever
writes to, truncates, renames, or deletes `config/long_term_memory.json`,
its Sprint 63 preservation backup, or any other real `config/*` file.

Run:
    python3 -m pytest tests/test_sprint64_memory_corruption_forensics.py -v
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import collections
import inspect

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.memory as memory_module  # noqa: E402
import luno.persistence as persistence_module  # noqa: E402
from luno import config as luno_config  # noqa: E402

_REAL_PROD_PATH = os.path.join(_ROOT, "config", "long_term_memory.json")
_REAL_BACKUP_PATH = os.path.join(
    _ROOT, "config", "backups",
    "long_term_memory.20260817T074000000000.pre_sprint63_forensic.json",
)


def _sha256_of(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = collections.Counter(data)
    total = len(data)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


# ============================================================================
# A - production file remains byte-identical (before/after this whole
#     module runs - this test intentionally sits FIRST so any later test
#     in this file that somehow touched it would be caught by comparing
#     against test N, not just this one snapshot)
# ============================================================================

_PROD_HASH_AT_COLLECTION = _sha256_of(_REAL_PROD_PATH)
_BACKUP_HASH_AT_COLLECTION = _sha256_of(_REAL_BACKUP_PATH)


def test_A_production_file_hash_matches_sprint63s_own_recorded_value():
    # NOTE (Sprint 64 correction): the SHA-256 string as originally copied
    # into Sprint 63/64 prose was missing its trailing hex digit (a 63-char
    # transcription typo of the real 64-char digest). The MD5 - independently
    # recorded and re-verified at every checkpoint across Sprints 55-64 - is
    # unaffected and is the primary drift guard; SHA-256 is corrected here to
    # the value actually produced by hashlib against the real file bytes.
    assert _PROD_HASH_AT_COLLECTION == (
        "be3a34ea7d44cf084b73ebba1a6596139acbf96bbd8d4d1c756fad1c943ed45a"
    ), "production file has changed since Sprint 63 - investigate before proceeding (Phase 12's own STOP rule)"
    assert hashlib.md5(open(_REAL_PROD_PATH, "rb").read()).hexdigest() == "c16525937a6bc063e182c1b6b120e42e"


# ============================================================================
# B - Sprint 63's preservation backup remains byte-identical to the
#     production file
# ============================================================================

def test_B_preservation_backup_still_byte_identical_to_production():
    assert _BACKUP_HASH_AT_COLLECTION == _PROD_HASH_AT_COLLECTION


# ============================================================================
# C - all discovered writer paths are enumerated (regression guard - if a
#     new persistence writer is ever added, this test's own list must be
#     updated, which is the point: no writer is silently forgotten)
# ============================================================================

_KNOWN_LONG_TERM_MEMORY_FILE_WRITERS = frozenset({
    # luno/memory.py's own hand-rolled writer (documented as the ONLY
    # production writer for config.LONG_TERM_MEMORY_FILE specifically -
    # NOT luno.persistence, which memory.py uses only for
    # config.SESSION_SUMMARIES_FILE, a different store).
    "luno.memory._save",
    "luno.memory._atomic_write_json",
    "luno.memory._backup_current_memory_file",
})


def test_C_only_known_writers_reference_config_long_term_memory_file():
    """Repo-wide structural check on `probe_memory_pipeline.py`, the one
    non-`luno.memory` module found (Phase 2) to reference
    `LONG_TERM_MEMORY_FILE` at all.

    CORRECTED CLAIM (Sprint 64, mid-investigation self-correction): earlier
    reasoning in this sprint asserted the script redirects
    `LONG_TERM_MEMORY_FILE` to an isolated temp path *before* importing
    `luno.memory`. Reading the actual source shows the opposite textual
    order - `import luno.memory as memory` (line 30) runs BEFORE the
    isolation loop that reassigns `luno_config.LONG_TERM_MEMORY_FILE`
    (lines 43-57). That earlier claim was wrong as stated and is corrected
    here rather than left in the record.

    The corrected, evidence-checked claim is narrower but still supports
    the same "never writes to the real file" conclusion, for a different
    reason:
      1. `luno/memory.py` never caches `config.LONG_TERM_MEMORY_FILE` into
         a module-level constant - every read/write site (`_save`,
         `_backup_current_memory_file`, `_load`, etc.) does a *live*
         `config.LONG_TERM_MEMORY_FILE` attribute lookup at call time
         (verified below). So mutating `luno_config.LONG_TERM_MEMORY_FILE`
         AFTER `luno.memory` is imported still fully redirects every
         future call.
      2. The only thing that runs against the ORIGINAL (non-redirected)
         path is `luno/memory.py`'s own module-level `_load()` call
         (`luno/memory.py:604`, "load sekali saat modul pertama kali
         diimpor") - which fires as a side effect of `import luno.memory`
         itself, before the probe's redirect loop executes. `_load()` is
         read-only by construction (falls back to backups, then an empty
         in-memory list, on any failure - see test_D and Sprint 63's own
         `_load()` audit); it has no code path that writes to disk. So
         this ordering produces one unintended READ of the real file at
         import time, never a write.
      3. All actual persistence (`_save()`) is triggered later, only from
         `run_conversation()` inside this script's own `if __name__ ==
         "__main__":` block - well after both the import and the redirect
         have completed - so every real save in this script's normal use
         targets the isolated temp path, not the production file.
    """
    import probe_memory_pipeline as probe  # noqa: F401  (import proves it doesn't fail; source inspected below)
    probe_src = inspect.getsource(probe)

    import_idx = probe_src.find("import luno.memory")
    redirect_idx = probe_src.find("setattr(luno_config, _attr")
    main_block_idx = probe_src.find('if __name__ == "__main__"')
    assert -1 not in (import_idx, redirect_idx, main_block_idx)

    # corrected textual-order claim: import precedes the redirect...
    assert import_idx < redirect_idx, (
        "expected import luno.memory to textually precede the isolation "
        "redirect in probe_memory_pipeline.py (this is the corrected "
        "claim - if this ever flips, the analysis above must be redone)"
    )
    # ...but BOTH precede the only code that can trigger a real save.
    assert redirect_idx < main_block_idx, (
        "the isolation redirect must still complete before any "
        "save-triggering code (run_conversation) can execute"
    )

    # live-lookup proof: no call site in luno/memory.py caches
    # config.LONG_TERM_MEMORY_FILE into a module-level name - every use is
    # a `config.LONG_TERM_MEMORY_FILE` (or `config.` alias) attribute
    # expression evaluated at call time.
    memory_src = inspect.getsource(memory_module)
    assert "_MEMORY_FILE = config.LONG_TERM_MEMORY_FILE" not in memory_src
    assert "_MEMORY_FILE = luno_config.LONG_TERM_MEMORY_FILE" not in memory_src
    live_lookup_sites = memory_src.count("config.LONG_TERM_MEMORY_FILE")
    assert live_lookup_sites >= 5, (
        f"expected multiple live config.LONG_TERM_MEMORY_FILE lookups in "
        f"luno/memory.py, found {live_lookup_sites}"
    )

    # _load() (the only thing that runs before the redirect) is read-only:
    # it must contain no disk-write primitive anywhere in its source. It
    # does legitimately call open() - but only ever in "r" (read) mode;
    # no write-mode open, no json.dump, no atomic-write helper, no raw
    # f.write().
    load_src = inspect.getsource(memory_module._load)
    for write_primitive in ("_atomic_write_json(", "json.dump(", "f.write(", 'open(path, "w"', "open(path, 'w'"):
        assert write_primitive not in load_src, (
            f"_load() must never call {write_primitive!r} - it is documented "
            f"read-only and this script's isolation depends on that being true"
        )
    assert 'open(path, "r"' in load_src or "open(path, 'r'" in load_src, (
        "expected _load()'s own file open to be explicitly read-mode"
    )


def test_C_persistence_module_is_never_called_with_the_long_term_memory_path():
    """`luno.persistence.atomic_write_json()` is the generic hardening
    helper other stores (VerifiedFactStore, HabitMemory, EpisodicMemory,
    RelationshipStore, DepthPreferenceStore, reminders, and memory.py's
    OWN session-summaries store) all funnel through - proves none of
    those callers' own hardcoded path constants can ever resolve to
    LONG_TERM_MEMORY_FILE (each is bound to a structurally different
    config attribute)."""
    other_store_path_constants = [
        "VERIFIED_FACTS_FILE", "HABIT_MEMORY_FILE", "EPISODIC_MEMORY_FILE",
        "RELATIONSHIP_STATE_FILE", "RESPONSE_DEPTH_PREFERENCE_FILE",
        "REMINDERS_FILE", "SESSION_SUMMARIES_FILE",
    ]
    for name in other_store_path_constants:
        assert hasattr(luno_config, name), f"expected config.{name} to exist"
        assert getattr(luno_config, name) != luno_config.LONG_TERM_MEMORY_FILE, (
            f"config.{name} must never alias config.LONG_TERM_MEMORY_FILE"
        )


# ============================================================================
# D - writer call graph is documented (this test IS the documentation,
#     kept executable so it can never silently drift from the real code)
# ============================================================================

def test_D_save_is_the_only_internal_entry_point_that_writes_to_disk():
    """Every mutation function in `luno.memory` is documented to funnel
    through `_save()` - this test proves `_save()` itself has exactly
    ONE call to a disk-writing primitive (`_atomic_write_json()`), so
    the call graph really is a single funnel, not multiple independent
    write sites that happen to converge by convention only."""
    src = inspect.getsource(memory_module._save)
    assert src.count("_atomic_write_json(") == 1
    assert "open(" not in src, "_save() must never open a file directly - only via _atomic_write_json()"


def test_D_atomic_write_json_never_writes_non_json_serializable_content():
    """Structural proof for Phase 9's own write-path audit: `_atomic_
    write_json()`'s only write call is `json.dump(data, f, ...)` - by
    construction, this can only ever emit valid UTF-8 JSON text (or
    raise TypeError first, writing nothing at all - see test_reproduction
    below). There is no code path in this function that could write
    arbitrary/binary bytes."""
    src = inspect.getsource(memory_module._atomic_write_json)
    assert "json.dump(" in src
    assert "f.write(" not in src, "expected the ONLY content-writing call to be json.dump(), not a raw f.write()"


# ============================================================================
# E - no test in this repository writes to the production memory file
#     (structural proof, not just this file's own convention)
# ============================================================================

def test_E_autouse_isolation_fixture_redirects_long_term_memory_file():
    import inspect as _inspect
    sys.path.insert(0, _THIS_DIR)
    import conftest as _conftest  # the real tests/conftest.py
    src = _inspect.getsource(_conftest.isolate_persistent_state)
    assert "LONG_TERM_MEMORY_FILE" in _inspect.getsource(_conftest) or \
        "_WRITABLE_STATE_ATTRS" in src
    assert "LONG_TERM_MEMORY_FILE" in "\n".join(_conftest._WRITABLE_STATE_ATTRS)


def test_E_this_modules_own_hashes_prove_no_mutation_occurred_mid_run():
    """A live, mid-run re-check (not just a before/after bookend) - if
    ANY test collected before this one in this file had somehow written
    to the real path, this would already have drifted."""
    assert _sha256_of(_REAL_PROD_PATH) == _PROD_HASH_AT_COLLECTION
    assert _sha256_of(_REAL_BACKUP_PATH) == _BACKUP_HASH_AT_COLLECTION


# ============================================================================
# F - temporary artifact search is read-only (proves the helper used
#     during this sprint's own investigation has no write/delete
#     capability, satisfying Phase 11's own helper requirement)
# ============================================================================

def _read_only_find_candidate_files(root: str, suffixes):
    """The exact kind of read-only helper this sprint's own Phase 4
    search used - listed here, executable, so its read-only nature is
    verifiable rather than asserted in prose. Never opens a file for
    writing, never deletes, never renames."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        if "__pycache__" in dirpath or "/.git" in dirpath:
            continue
        for name in filenames:
            if any(name.endswith(suf) for suf in suffixes):
                found.append(os.path.join(dirpath, name))
    return found


def test_F_temporary_artifact_search_finds_none_and_mutates_nothing():
    before = _sha256_of(_REAL_PROD_PATH)
    results = _read_only_find_candidate_files(
        _ROOT, (".tmp", ".bak", ".backup", ".old", ".recovery", ".partial", ".json.tmp")
    )
    after = _sha256_of(_REAL_PROD_PATH)
    assert after == before, "a read-only search must never touch the production file"
    assert results == [], f"expected zero stray temp/backup artifacts in the repo, found: {results}"


# ============================================================================
# G - MIT LICENSE fingerprint search is read-only
# ============================================================================

def test_G_license_fingerprint_search_is_read_only_and_uses_exact_bytes():
    with open(_REAL_PROD_PATH, "rb") as f:
        data = f.read()
    tail_fingerprint = data[1536:]  # the exact 313-byte tail, read-only

    before = _sha256_of(_REAL_PROD_PATH)
    # search a small, bounded, known-readable location only (this test's
    # own point is to prove the SEARCH METHOD is read-only, not to
    # re-run this sprint's own full site-packages sweep, which is
    # documented separately as a one-time, bounded investigation step)
    candidate_dir = os.path.join(_ROOT, "docs")
    matches = []
    for dirpath, _, filenames in os.walk(candidate_dir):
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                with open(p, "rb") as f:
                    content = f.read()
            except Exception:
                continue
            if tail_fingerprint in content:
                matches.append(p)
    after = _sha256_of(_REAL_PROD_PATH)
    assert after == before
    # this sprint's own change-impact doc quotes the phrase (not the
    # full 313-byte fingerprint verbatim with identical wrapping), so
    # zero or a small number of matches is expected here; the assertion
    # is about read-only behavior, not about finding a specific count.
    assert isinstance(matches, list)


# ============================================================================
# H - filesystem metadata inspection is read-only
# ============================================================================

def test_H_metadata_inspection_never_touches_mtime_or_content():
    before_stat = os.stat(_REAL_PROD_PATH)
    before_hash = _sha256_of(_REAL_PROD_PATH)

    # the same kind of metadata read this sprint's own Phase 1 used
    _ = (before_stat.st_size, before_stat.st_mtime_ns, before_stat.st_ctime_ns,
         before_stat.st_ino, before_stat.st_mode, before_stat.st_uid, before_stat.st_gid)
    assert not os.path.islink(_REAL_PROD_PATH)

    after_stat = os.stat(_REAL_PROD_PATH)
    after_hash = _sha256_of(_REAL_PROD_PATH)
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_ino == after_stat.st_ino
    assert before_hash == after_hash


def test_H_every_original_config_file_shares_the_same_bulk_extraction_permission():
    """New Sprint 64 finding: the production file's read-only (444)
    permission is NOT a corruption-specific protection - EVERY
    still-untouched original config/source file in this checkout
    (including main_runtime_demo.py, luno/memory.py, ARCHITECTURE_GUARD.
    md) shares the identical 444 mode, while files that have been
    actively rewritten during this sandbox's own test/sprint activity
    (config/relationship_state.json, config/vision_memory.sqlite3) do
    not. This is read-only evidence about a packaging/extraction-wide
    permission pattern, not a targeted safeguard."""
    untouched_config_files = [
        f for f in os.listdir(os.path.join(_ROOT, "config"))
        if f.endswith(".json") and f not in ("relationship_state.json",)
    ]
    modes = set()
    for name in untouched_config_files:
        p = os.path.join(_ROOT, "config", name)
        modes.add(oct(os.stat(p).st_mode)[-3:])
    assert modes == {"444"}, f"expected every untouched config/*.json file to share mode 444, got {modes}"


# ============================================================================
# I - forensic reproduction uses a temporary directory only (Phase 10 -
#     a NEGATIVE-CONTROL experiment: proves luno.memory's own write path
#     structurally CANNOT produce the observed artifact shape, under an
#     interrupted-write scenario, entirely inside tmp_path)
# ============================================================================

def test_I_interrupted_write_never_produces_binary_or_license_like_content(monkeypatch, tmp_path):
    """Simulates a process being killed AFTER the temp file is written
    but BEFORE `os.replace()` swaps it in - the closest real-world
    analogue to 'write interrupted' this code's own design allows for
    (anything killed earlier just never gets this far; anything killed
    later already fully replaced the file with complete, valid JSON).
    Proves: (a) the production-path file is completely untouched, and
    (b) the leftover .tmp file itself - even though abandoned - is
    always well-formed UTF-8 JSON text, NEVER high-entropy binary
    content or embedded unrelated plaintext, because `json.dump()` is
    the only thing that ever writes bytes into it."""
    target = tmp_path / "long_term_memory.json"
    original_content = json.dumps([{"id": "keep", "text": "original", "created_at": "2026-01-01T00:00:00"}])
    target.write_text(original_content, encoding="utf-8")

    real_replace = os.replace

    def _boom(*a, **kw):
        raise OSError("simulated process kill / crash right before os.replace()")

    monkeypatch.setattr(os, "replace", _boom)

    memory_module._memories = [{"id": "new", "text": "should never fully land", "created_at": "2026-01-02T00:00:00"}]
    try:
        memory_module._atomic_write_json(str(target), memory_module._memories)
        assert False, "expected the simulated crash to propagate"
    except OSError:
        pass

    # (a) production-path content is untouched
    assert target.read_text(encoding="utf-8") == original_content

    # (b) inspect whatever .tmp file(s) exist - _atomic_write_json's own
    # cleanup should have removed it, but even if cleanup itself were
    # somehow bypassed, prove what WOULD be there is still just JSON text.
    leftover_tmp_files = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    for name in leftover_tmp_files:
        with open(tmp_path / name, "rb") as f:
            leftover_bytes = f.read()
        # must decode as UTF-8 (never high-entropy binary)
        leftover_bytes.decode("utf-8")
        assert b"Permission is hereby granted" not in leftover_bytes
        run_len = _longest_same_byte_run(leftover_bytes)
        assert run_len < 32, "a legitimate JSON write should never contain a long same-byte run"

    monkeypatch.setattr(os, "replace", real_replace)  # explicit, though monkeypatch would revert anyway


def _longest_same_byte_run(data: bytes) -> int:
    if not data:
        return 0
    max_run, cur = 1, 1
    for i in range(1, len(data)):
        if data[i] == data[i - 1]:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 1
    return max_run


def test_I_reproduction_of_observed_structure_from_this_codebase_not_possible():
    """Documents Phase 10's own explicit outcome: this sprint found NO
    writer within this codebase capable of producing the observed
    artifact shape (high-entropy region + long NUL run + embedded
    third-party plaintext), so no attempt was made to force a
    reproduction from a non-existent candidate - forcing one would only
    fabricate a misleading result. This test exists so that conclusion
    is itself checkable: `_memories` (the only data `_atomic_write_json`
    is ever called with in production) is always a `list`, and a `list`
    of the `dict` shapes this module produces can never Round-trip through
    `json.dump` into non-UTF8 bytes."""
    assert isinstance(memory_module._memories, list)
    # every real _save() call site passes memory_module._memories (a
    # module-level list) - never an arbitrary/external byte string.
    save_src = inspect.getsource(memory_module._save)
    assert "_memories" in save_src


# ============================================================================
# J - no persistence mutation during this entire test file's run
#     (bookend check - compares against the collection-time hash
#     captured as module-level constants at the very top of this file)
# ============================================================================

def test_J_no_persistence_mutation_across_this_entire_test_files_run():
    assert _sha256_of(_REAL_PROD_PATH) == _PROD_HASH_AT_COLLECTION
    assert _sha256_of(_REAL_BACKUP_PATH) == _BACKUP_HASH_AT_COLLECTION
    # also confirm no NEW long_term_memory.*.json backup was created by
    # this test file's own run (would indicate an accidental _save() call)
    backup_dir = os.path.join(_ROOT, "config", "backups")
    long_term_backups = sorted(
        f for f in os.listdir(backup_dir)
        if f.startswith("long_term_memory.") and f.endswith(".json")
    )
    assert long_term_backups == ["long_term_memory.20260817T074000000000.pre_sprint63_forensic.json"], (
        f"expected exactly the Sprint 63 preservation backup and nothing else, found: {long_term_backups}"
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
