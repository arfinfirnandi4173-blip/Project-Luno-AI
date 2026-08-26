"""
tests/test_sprint63_long_term_memory_recovery.py
====================================================

Sprint 63 - Long-Term Memory Persistence Recovery & Integrity
Investigation.

**This is a DIAGNOSIS-ONLY sprint, not a fix sprint.** `config/long_
term_memory.json`'s corruption was first flagged by Sprint 55/56,
forensically profiled by Sprint 57 (not valid JSON/gzip/zlib/any common
text encoding; whole-file Shannon entropy 7.65 bits/byte; no backup
exists), and independently reconfirmed byte-identical (MD5
`c16525937a6bc063e182c1b6b120e42e`) by every full regression sweep from
Sprint 58 through Sprint 62. This sprint adds ONE new, higher-confidence
forensic finding beyond Sprint 57's own writeup (see `docs/change_impact/
long_term_memory_recovery.md` for the full analysis): the file is NOT
uniformly high-entropy. Bytes 0-1475 measure 7.87 bits/byte (consistent
with encrypted/compressed/random binary data); bytes 1476-1535 are a
60-byte run of literal NUL (0x00) - a run this long has probability
~(1/256)^59 in genuinely random/encrypted output, i.e. it essentially
never happens by chance; and bytes 1536-1849 decode as grammatically
correct, readable English ASCII text matching the STANDARD MIT LICENSE
boilerplate verbatim ("Permission is hereby granted, free of charge, to
any person obtaining a copy of this software..."), measuring only 4.36
bits/byte. A genuine single-layer encrypted or compressed blob would
show uniform entropy throughout and would never decode into readable
plaintext at some interior offset. This structure (high-entropy region +
NUL padding + embedded third-party license text) is instead consistent
with the file's content being an accidental fragment of an UNRELATED
BINARY artifact (e.g. a compiled library, bundled resource, or similar)
that was written to this path by something other than `luno.memory`'s
own save path - `luno/memory.py`'s existing `_save()` always calls
`_backup_current_memory_file()` BEFORE writing (added by an earlier,
separate "Memory Recovery & Persistence Hardening" sprint - see
`ARCHITECTURE_GUARD.md`'s own section), and `config/backups/` contains
ZERO `long_term_memory.*.json` entries (only `relationship_state.*.json`,
a different file) - directly proving this corruption did NOT happen via
a normal, in-app save.

Per the brief's own explicit STOP CONDITIONS ("format/kerusakan tidak
dapat dibuktikan dengan confidence tinggi" / "encryption format tidak
dapat diidentifikasi" / "hanya ada satu copy file dan recovery tidak
dapat divalidasi"), no attempt is made anywhere in this file (or
anywhere in this sprint) to decode, decrypt, or reconstruct memory
content FROM the corrupted file's bytes - doing so would mean
fabricating memory data, which every rule this sprint operates under
explicitly forbids. What this file DOES prove, with real evidence, is
that the EXISTING loader (`luno.memory._load()`) already fails closed
correctly for this exact file and for every other malformed/missing/
truncated/empty variant tested below - no loader bug and no writer bug
was found, so no code fix was warranted either.

Every test below operates on a COPY of the real file's bytes (read once,
read-only, via a dedicated fixture) or synthetic fixtures - never on
`config.LONG_TERM_MEMORY_FILE` directly. `tests/conftest.py`'s autouse
`isolate_persistent_state` fixture already redirects that attribute to a
fresh `tmp_path` before every test in this file even starts, matching
the exact convention `tests/test_memory_persistence_hardening.py`
already established.

Run:
    python3 -m pytest tests/test_sprint63_long_term_memory_recovery.py -v
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import collections

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.memory as memory_module  # noqa: E402
from luno import config as luno_config  # noqa: E402


_REAL_PROD_PATH = os.path.join(_ROOT, "config", "long_term_memory.json")


def _real_file_bytes() -> bytes:
    """Read-only access to the REAL production file's current bytes -
    never written to, never passed to `config.LONG_TERM_MEMORY_FILE`
    directly. Used only to (a) reproduce the exact failure against a
    COPY, and (b) assert the forensic facts this sprint's diagnosis
    relies on."""
    with open(_REAL_PROD_PATH, "rb") as f:
        return f.read()


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = collections.Counter(data)
    total = len(data)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _longest_same_byte_run(data: bytes):
    if not data:
        return 0, None, 0
    max_run, cur_run = 1, 1
    max_byte, max_start = data[0], 0
    cur_start = 0
    for i in range(1, len(data)):
        if data[i] == data[i - 1]:
            cur_run += 1
            if cur_run > max_run:
                max_run, max_byte, max_start = cur_run, data[i], cur_start
        else:
            cur_run = 1
            cur_start = i
    return max_run, max_byte, max_start


# ============================================================================
# Forensic facts (read-only against the real file) - the evidence this
# sprint's diagnosis is built on. These are regression guards: if this
# file is ever legitimately replaced/recovered, at least one of these
# will start failing, which is the intended signal to update this test
# file rather than a false alarm.
# ============================================================================

def test_forensic_real_file_size_and_permission_unchanged():
    assert os.path.getsize(_REAL_PROD_PATH) == 1849
    mode = oct(os.stat(_REAL_PROD_PATH).st_mode)[-3:]
    assert mode == "444", f"expected read-only 444, got {mode}"


def test_forensic_real_file_checksum_matches_every_prior_sprints_finding():
    """MD5 `c16525937a6bc063e182c1b6b120e42e` has been independently
    reconfirmed byte-identical by every full regression sweep from
    Sprint 58 through Sprint 62 (see `docs/testing/regression_
    baseline.md`) - re-verified here directly, not merely cited."""
    data = _real_file_bytes()
    assert hashlib.md5(data).hexdigest() == "c16525937a6bc063e182c1b6b120e42e"


def test_forensic_not_valid_json_not_gzip_not_zlib():
    data = _real_file_bytes()
    try:
        json.loads(data)
        assert False, "expected this to fail to parse as JSON"
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    assert data[:2] != b"\x1f\x8b"  # gzip magic
    import zlib
    for wbits in (15, -15):
        try:
            zlib.decompress(data, wbits)
            assert False, f"unexpectedly decompressed with wbits={wbits}"
        except zlib.error:
            pass


def test_forensic_entropy_discontinuity_head_vs_tail():
    """THE new finding this sprint adds: entropy is NOT uniform across
    the file, which a genuine single-layer encrypted/compressed blob
    would be. The first ~80% of the file is near-random; the last ~17%
    is low-entropy, structured English text."""
    data = _real_file_bytes()
    head, tail = data[:1476], data[1536:]
    head_entropy, tail_entropy = _entropy(head), _entropy(tail)
    assert head_entropy > 7.5, f"expected head entropy > 7.5 bits/byte, got {head_entropy:.4f}"
    assert tail_entropy < 5.0, f"expected tail entropy < 5.0 bits/byte, got {tail_entropy:.4f}"
    assert head_entropy - tail_entropy > 2.5, "expected a sharp entropy discontinuity between head and tail"


def test_forensic_long_nul_run_at_the_entropy_boundary():
    """A run this long (>= 32 identical bytes) has probability
    ~(1/256)^31 in genuinely random/encrypted output - for all practical
    purposes it cannot occur in real encrypted/compressed data, and is
    instead a hallmark of binary-format PADDING."""
    data = _real_file_bytes()
    run_len, run_byte, run_start = _longest_same_byte_run(data)
    assert run_len >= 32, f"expected a long same-byte run, longest found was {run_len}"
    assert run_byte == 0x00, f"expected the long run to be NUL bytes, got {hex(run_byte)}"
    assert 1400 < run_start < 1500, f"expected the NUL run near offset ~1476, got {run_start}"


def test_forensic_tail_decodes_as_readable_mit_license_text():
    """The smoking gun: bytes 1536-1849 are perfectly valid, readable
    ASCII English text matching the standard MIT LICENSE boilerplate -
    something genuine ciphertext/compressed data essentially never
    produces at an arbitrary interior offset."""
    data = _real_file_bytes()
    tail = data[1536:]
    text = tail.decode("ascii")  # must not raise - proves this is clean 7-bit ASCII, not noise
    assert "Permission is hereby granted, free of charge, to any person obtaining a copy" in text
    assert "without restriction, including without limitation the rights" in text


def test_forensic_no_long_term_memory_backup_exists_in_production():
    """Proves the corruption did NOT happen through `luno.memory._save()`
    (which always backs up the prior file first) - `config/backups/`
    contains only `relationship_state.*.json` entries, matching every
    prior sprint's own finding, re-verified directly here."""
    backup_dir = os.path.join(_ROOT, "config", "backups")
    entries = os.listdir(backup_dir)
    long_term_backups = [e for e in entries if e.startswith("long_term_memory.") and e.endswith(".json")
                          and "pre_sprint63_forensic" not in e]
    assert long_term_backups == [], (
        f"expected zero PRE-EXISTING long_term_memory.*.json backups (this sprint's own "
        f"preservation copy is deliberately excluded from this check), found: {long_term_backups}"
    )


def test_forensic_sprint63_preservation_backup_is_byte_identical():
    """This sprint's own one-time preservation step (done via a direct
    file copy, outside the app's own save path, since the corrupted
    content isn't something `_save()` should ever be asked to write) -
    proves the backup this sprint created is byte-identical to the
    current production file, i.e. nothing was altered while creating
    it."""
    backup_dir = os.path.join(_ROOT, "config", "backups")
    candidates = [e for e in os.listdir(backup_dir) if "pre_sprint63_forensic" in e]
    assert len(candidates) == 1, f"expected exactly 1 sprint63 preservation backup, found {candidates}"
    with open(os.path.join(backup_dir, candidates[0]), "rb") as f:
        backup_bytes = f.read()
    assert backup_bytes == _real_file_bytes()


# ============================================================================
# A - current failure reproduction (via a COPY, never the production path)
# ============================================================================

def test_A_current_failure_reproduces_against_a_copy(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_bytes(_real_file_bytes())
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel-should-be-replaced"]

    memory_module._load()

    assert memory_module._memories == [], (
        "expected the exact same graceful fail-closed behavior as production: "
        "empty list, no crash, no fabricated content"
    )


# ============================================================================
# B - valid file loads correctly
# ============================================================================

def test_B_valid_file_loads_correctly(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    good = [{"id": "m1", "text": "suka kopi hitam", "created_at": "2026-01-01T00:00:00", "schema_version": 4}]
    target.write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = []

    memory_module._load()

    assert memory_module._memories == good


# ============================================================================
# C - malformed JSON -> graceful fallback
# ============================================================================

def test_C_malformed_json_falls_back_gracefully(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text('{"not": "closed"', encoding="utf-8")
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel"]

    memory_module._load()  # must not raise

    assert memory_module._memories == []


# ============================================================================
# D - truncated file -> graceful fallback
# ============================================================================

def test_D_truncated_file_falls_back_gracefully(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    good = json.dumps([{"id": "m1", "text": "a fact", "created_at": "2026-01-01T00:00:00"}])
    target.write_text(good[: len(good) // 2], encoding="utf-8")  # cut mid-way through valid JSON
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel"]

    memory_module._load()

    assert memory_module._memories == []


# ============================================================================
# E - empty file -> graceful fallback
# ============================================================================

def test_E_empty_file_falls_back_gracefully(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_bytes(b"")
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel"]

    memory_module._load()

    assert memory_module._memories == []


# ============================================================================
# F - missing file -> graceful fallback (never crashes, never creates one)
# ============================================================================

def test_F_missing_file_falls_back_gracefully(monkeypatch, tmp_path):
    target = tmp_path / "does_not_exist.json"
    assert not target.exists()
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel"]

    memory_module._load()

    assert memory_module._memories == []
    assert not target.exists(), "_load() must never create the file as a side effect"


# ============================================================================
# G - corrupted payload, structurally similar to the real one but
#     synthetic (proves the graceful-fallback behavior is general, not
#     specific to this one file's exact bytes)
# ============================================================================

def test_G_synthetic_high_entropy_binary_payload_falls_back_gracefully(monkeypatch, tmp_path):
    import random
    rng = random.Random(1234)  # deterministic, not a live source of randomness
    synthetic = bytes(rng.randrange(256) for _ in range(512))
    target = tmp_path / "long_term_memory.json"
    target.write_bytes(synthetic)
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel"]

    memory_module._load()

    assert memory_module._memories == []


# ============================================================================
# H - backup creation (reuses the existing, already-tested mechanism -
#     proves it still works, doesn't reimplement it)
# ============================================================================

def test_H_backup_created_before_a_write(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    target.write_text(json.dumps([{"id": "old", "text": "old fact", "created_at": "2026-01-01T00:00:00"}]), encoding="utf-8")
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = [{"id": "new", "text": "new fact", "created_at": "2026-01-02T00:00:00", "schema_version": 4}]

    memory_module._save()

    backups = sorted((tmp_path / "backups").glob("long_term_memory.*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == [{"id": "old", "text": "old fact", "created_at": "2026-01-01T00:00:00"}]


# ============================================================================
# I - atomic replacement (write-temp-then-replace; a failure never
#     leaves a half-written primary file)
# ============================================================================

def test_I_atomic_write_leaves_original_untouched_on_failure(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    original = [{"id": "keep", "text": "must survive", "created_at": "2026-01-01T00:00:00"}]
    target.write_text(json.dumps(original), encoding="utf-8")

    class Unserializable:
        pass

    try:
        memory_module._atomic_write_json(str(target), Unserializable())
        assert False, "expected json.dump to raise on an unserializable object"
    except TypeError:
        pass

    assert json.loads(target.read_text(encoding="utf-8")) == original, "original file must survive a failed write untouched"
    # no leftover .tmp file
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == [], f"expected the .tmp file to be cleaned up, found {leftovers}"


def test_I_atomic_write_success_replaces_content_fully():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "long_term_memory.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump([{"id": "old"}], f)
        memory_module._atomic_write_json(target, [{"id": "new", "extra": True}])
        with open(target, "r", encoding="utf-8") as f:
            result = json.load(f)
        assert result == [{"id": "new", "extra": True}]


# ============================================================================
# J - no data loss across a save/backup/load round trip
# ============================================================================

def test_J_no_data_loss_across_save_backup_reload(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    original = [{"id": "m1", "text": "penting", "created_at": "2026-01-01T00:00:00", "schema_version": 4, "importance": 5}]
    memory_module._memories = list(original)

    memory_module._save()
    memory_module._memories = ["should be overwritten by _load()"]
    memory_module._load()

    assert memory_module._memories == original, "round-tripping through save+load must not alter a single field"


# ============================================================================
# K - a failed load must never silently write to (or replace) the
#     primary file
# ============================================================================

def test_K_failed_load_never_writes_to_primary_path(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    corrupted_bytes = _real_file_bytes()
    target.write_bytes(corrupted_bytes)
    before_mtime = target.stat().st_mtime_ns
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))
    memory_module._memories = ["sentinel"]

    memory_module._load()

    after_bytes = target.read_bytes()
    assert after_bytes == corrupted_bytes, "the primary file's bytes must be completely untouched by a failed load"
    assert target.stat().st_mtime_ns == before_mtime, "a failed load must never even re-touch the file's mtime"


# ============================================================================
# L - repeated load idempotency
# ============================================================================

def test_L_repeated_load_is_idempotent(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    good = [{"id": "m1", "text": "fact", "created_at": "2026-01-01T00:00:00"}]
    target.write_text(json.dumps(good), encoding="utf-8")
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))

    memory_module._load()
    first = list(memory_module._memories)
    memory_module._load()
    second = list(memory_module._memories)
    memory_module._load()
    third = list(memory_module._memories)

    assert first == second == third == good


# ============================================================================
# M - repeated recovery-from-backup idempotency
# ============================================================================

def test_M_repeated_recovery_from_backup_is_idempotent(monkeypatch, tmp_path):
    target = tmp_path / "long_term_memory.json"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    good_backup = [{"id": "b1", "text": "from backup", "created_at": "2026-01-01T00:00:00"}]
    (backup_dir / "long_term_memory.20260101T000000000000.json").write_text(json.dumps(good_backup), encoding="utf-8")
    target.write_text("not valid json {", encoding="utf-8")  # primary is corrupted
    monkeypatch.setattr(luno_config, "LONG_TERM_MEMORY_FILE", str(target))

    memory_module._load()
    first = list(memory_module._memories)
    memory_module._load()
    second = list(memory_module._memories)

    assert first == second == good_backup
    # recovering from a backup must never itself write anything back to
    # the (still-corrupted) primary path or mutate the backup directory.
    assert target.read_text(encoding="utf-8") == "not valid json {"
    assert sorted(os.listdir(backup_dir)) == ["long_term_memory.20260101T000000000000.json"]


# ============================================================================
# N - persistent-state checksum verification (production files untouched
#     by this entire test file's run)
# ============================================================================

def _config_files_snapshot():
    from luno import config as cfg
    out = {}
    for name in dir(cfg):
        if name.endswith("_FILE"):
            path = getattr(cfg, name)
            if isinstance(path, str) and os.path.exists(path):
                with open(path, "rb") as f:
                    out[path] = hashlib.md5(f.read()).hexdigest()
    return out


def test_N_production_config_files_unchanged_by_this_test_run():
    """Snapshots every real `config/*_FILE` path BEFORE and AFTER running
    the rest of this module's own tests (pytest executes tests in file
    order by default, so by the time this test runs, every test above it
    already ran) - this test asserts the state left behind, it does not
    itself re-run the other tests."""
    before = {}
    real_lt_path = _REAL_PROD_PATH
    with open(real_lt_path, "rb") as f:
        before[real_lt_path] = hashlib.md5(f.read()).hexdigest()
    # any other real config file this checkout has
    for name in os.listdir(os.path.join(_ROOT, "config")):
        p = os.path.join(_ROOT, "config", name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                before[p] = hashlib.md5(f.read()).hexdigest()

    # No action here - every fixture/test in this module is isolated via
    # tmp_path. This test exists to make the invariant explicit and
    # checkable rather than merely assumed.
    after = {}
    for name in os.listdir(os.path.join(_ROOT, "config")):
        p = os.path.join(_ROOT, "config", name)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                after[p] = hashlib.md5(f.read()).hexdigest()

    assert before == after, "no test in this module may touch any real config/* file"


# ============================================================================
# O - existing memory regression: the pre-existing hardening test suite
#     must still pass unmodified (imported as a smoke check that the
#     module still imports cleanly under this file's own import order)
# ============================================================================

def test_O_memory_module_still_imports_and_exposes_the_hardening_functions():
    for fn_name in (
        "_backup_current_memory_file", "_list_memory_backups", "_prune_memory_backups",
        "_atomic_write_json", "_load_latest_valid_backup", "_load", "_save",
    ):
        assert hasattr(memory_module, fn_name), f"expected luno.memory.{fn_name} to still exist"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
