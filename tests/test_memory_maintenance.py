"""
test_memory_maintenance.py
============================

MEMORY LIFECYCLE & MAINTENANCE ENGINE sprint - test suite for the
ADDITIONS this sprint made to `luno/memory.py`: usage tracking
(`retrieval_count`/`last_retrieved_at`), conservative frequency-driven
reinforcement, the deterministic maintenance planner
(`analyze_memory_maintenance()`), explicit execution
(`apply_maintenance_plan()`), dry-run preview, health report, protected-
memory rules, and the 8 explicit manual commands.

Does NOT duplicate `tests/test_memory_intelligence.py`'s importance/
lifecycle coverage or `tests/test_memory_conflict.py`'s conflict
classification coverage - both are reused, unchanged, by this sprint's
own logic, and are already covered elsewhere.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND
resets `luno.memory._memories` to `[]` for every test in this file - no
manual save/restore boilerplate needed, and no test here can ever touch
Vinn's real production `config/long_term_memory.json`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import luno.memory as memory
from luno.memory_retrieval.models import RelevantMemory


def _entry(text, importance=2, days_ago=1, source="llm_auto", category="other",
           id_=None, conflict_status=None, conflict_group=None, history=None,
           retrieval_count=None, last_retrieved_at=None, archived_by_maintenance=None):
    ts = (datetime.now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    entry = {
        "id": id_ or f"e-{abs(hash(text)) % 100000}",
        "text": text, "category": category, "importance": importance, "source": source,
        "created_at": ts, "updated_at": ts, "history": history or [],
    }
    if conflict_status:
        entry["conflict_status"] = conflict_status
    if conflict_group:
        entry["conflict_group"] = conflict_group
    if retrieval_count is not None:
        entry["retrieval_count"] = retrieval_count
    if last_retrieved_at is not None:
        entry["last_retrieved_at"] = last_retrieved_at
    if archived_by_maintenance is not None:
        entry["archived_by_maintenance"] = archived_by_maintenance
    return entry


def _rm(entry):
    """A minimal `RelevantMemory` shaped exactly like what
    `make_manual_memory_source()` produces - `source="manual_memory"`,
    `raw=<the entry dict>`."""
    return RelevantMemory(text=entry["text"], source="manual_memory", score=1.0, raw=entry)


# ─────────────────────────────────────────────
# A - lifecycle classification (reused, unchanged - confirming the
# archive-override flag composes correctly with the existing function)
# ─────────────────────────────────────────────


def test_lifecycle_still_pure_and_unaffected_without_the_new_flag():
    entry = _entry("some fact", importance=2, days_ago=1)
    assert memory.compute_lifecycle(entry) == "active"


def test_lifecycle_archived_flag_overrides_fresh_age():
    entry = _entry("some fact", importance=2, days_ago=0, archived_by_maintenance=True)
    assert memory.compute_lifecycle(entry) == "archived"


# ─────────────────────────────────────────────
# B/C - stale / archived detection via the planner
# ─────────────────────────────────────────────


def test_stale_low_usage_entry_recommended_for_archive():
    # importance=1 thresholds are (14, 60) days - 30 days lands solidly
    # in the "stale" band (not yet "archived" by age alone), matching
    # `tests/test_memory_intelligence.py`'s own precedent for this exact
    # importance/age combination.
    memory._memories.append(_entry("proyek lama yang jarang dibahas", importance=1, days_ago=30))
    assert memory.compute_lifecycle(memory._memories[0]) == "stale"
    plan = memory.analyze_memory_maintenance()
    assert plan[0]["action"] == "archive"


def test_already_archived_entry_recommended_keep_not_archive_again():
    memory._memories.append(_entry("sudah diarsipkan", importance=1, days_ago=1, archived_by_maintenance=True))
    plan = memory.analyze_memory_maintenance()
    assert plan[0]["action"] == "keep"
    assert "already archived" in plan[0]["reason"]


# ─────────────────────────────────────────────
# D/E/F/G - protected memory
# ─────────────────────────────────────────────


def test_importance_4_is_protected_and_kept():
    memory._memories.append(_entry("core memory", importance=4, days_ago=5000))
    plan = memory.analyze_memory_maintenance()
    assert plan[0]["action"] == "keep"
    assert "protected" in plan[0]["reason"]


def test_explicit_user_marked_important_via_mark_last_memory_important_is_protected():
    memory.add_memory("aku suka kopi hitam")
    entry = memory.mark_last_memory_important()
    assert entry["importance"] == 4
    plan = memory.analyze_memory_maintenance()
    marked = next(p for p in plan if p["memory_id"] == entry["id"])
    assert marked["action"] == "keep"


def test_verified_facts_are_structurally_unreachable_by_maintenance(tmp_path):
    from luno.memory_guard import VerifiedFactStore
    from luno.tool_manager.result import ToolResult

    store = VerifiedFactStore(path=str(tmp_path / "verified_facts.json"))
    result = ToolResult(success=True, tool="home_assistant", action="turn_on",
                         message="ok", data={"entity_id": "light.kamar", "target": "lampu kamar"})
    store.record(result, tool_name="home_assistant", request_id="r1")
    before = store.all_facts()

    memory._memories.append(_entry("lampu kamar gampang dinyalain", importance=1, days_ago=100))
    memory.analyze_memory_maintenance()
    memory.apply_maintenance_plan(memory.analyze_memory_maintenance())

    assert store.all_facts() == before


def test_unresolved_conflict_is_protected_review_not_archived():
    memory.add_memory("Aku pakai Windows 11")
    memory.add_memory("Aku pakai Ubuntu")
    groups = memory.list_conflicts()
    assert len(groups) == 1
    plan = memory.analyze_memory_maintenance()
    conflict_ids = {m["id"] for m in groups[0]}
    for item in plan:
        if item["memory_id"] in conflict_ids:
            assert item["action"] == "review"


# ─────────────────────────────────────────────
# H/I/J - retrieval count / last_retrieved_at / failure doesn't count
# ─────────────────────────────────────────────


def test_retrieval_count_increments_on_successful_result():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.record_memory_usage([_rm(entry)])
    live = memory.get_memory(entry["id"])
    assert live["retrieval_count"] == 1
    assert live["last_retrieved_at"] is not None


def test_last_retrieved_at_updates_on_each_use():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.record_memory_usage([_rm(entry)], now=datetime(2020, 1, 1))
    first = memory.get_memory(entry["id"])["last_retrieved_at"]
    memory.record_memory_usage([_rm(entry)], now=datetime(2021, 1, 1))
    second = memory.get_memory(entry["id"])["last_retrieved_at"]
    assert first != second
    assert second.startswith("2021")


def test_empty_retrieval_result_never_increments_anything():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.record_memory_usage([])
    live = memory.get_memory(entry["id"])
    assert live.get("retrieval_count", 0) == 0


def test_non_manual_memory_source_results_are_ignored():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    vision_rm = RelevantMemory(text="cup on the desk", source="vision_objects", score=1.0, raw={"id": entry["id"]})
    memory.record_memory_usage([vision_rm])
    live = memory.get_memory(entry["id"])
    assert live.get("retrieval_count", 0) == 0


def test_merely_existing_in_store_never_counts_as_usage():
    """Step 4's own explicit distinction - saving a memory does not, by
    itself, count as a retrieval."""
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    assert entry.get("retrieval_count", 0) == 0


# ─────────────────────────────────────────────
# K/L - reinforcement: repeated retrieval helps, but never alone reaches 4
# ─────────────────────────────────────────────


def test_repeated_retrieval_reinforces_importance_up_to_cap():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")  # importance 2
    for _ in range(5):
        memory.record_memory_usage([_rm(entry)])
    live = memory.get_memory(entry["id"])
    assert live["importance"] == 3  # bumped once at the 5th retrieval
    assert live["retrieval_count"] == 5


def test_frequency_alone_never_reaches_importance_4():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")  # importance 2
    for _ in range(200):
        memory.record_memory_usage([_rm(entry)])
    live = memory.get_memory(entry["id"])
    assert live["importance"] == 3  # capped, never 4, no matter how many retrievals
    assert live["retrieval_count"] == 200


def test_already_high_importance_entry_untouched_by_frequency():
    entry = memory.add_memory("ini penting banget, aku alergi kacang")  # importance 4 (explicit)
    for _ in range(20):
        memory.record_memory_usage([_rm(entry)])
    live = memory.get_memory(entry["id"])
    assert live["importance"] == 4


# ─────────────────────────────────────────────
# M/N/O - exact / near / ambiguous duplicates
# ─────────────────────────────────────────────


def test_exact_duplicate_pair_recommended_for_consolidation():
    a = _entry("GPU laptop pakai RTX 3060 Ti", importance=2, days_ago=1, id_="a1")
    b = _entry("GPU laptop pakai RTX 3060 Ti", importance=1, days_ago=1, id_="a2")
    memory._memories.extend([a, b])
    plan = {p["memory_id"]: p for p in memory.analyze_memory_maintenance()}
    assert plan["a2"]["action"] == "consolidate"
    assert plan["a2"]["consolidate_with"] == "a1"
    assert plan["a2"]["confidence"] >= 0.9


def test_near_duplicate_pair_recommended_for_consolidation():
    a = _entry("GPU laptop pakai RTX 3060 Ti buat gaming", importance=2, days_ago=1,
               category="technical_fact", id_="n1")
    b = _entry("Laptop GPU pakai RTX 3060 Ti buat gaming enak", importance=1, days_ago=1,
               category="technical_fact", id_="n2")
    memory._memories.extend([a, b])
    plan = {p["memory_id"]: p for p in memory.analyze_memory_maintenance()}
    consolidated = [p for p in plan.values() if p["action"] == "consolidate"]
    assert len(consolidated) == 1
    assert consolidated[0]["consolidate_with"] == "n1"  # higher importance survives


def test_ambiguous_pair_never_silently_consolidated():
    memory.add_memory("Aku pakai Windows 11")
    memory.add_memory("Aku pakai Ubuntu")
    plan = memory.analyze_memory_maintenance()
    assert not any(p["action"] == "consolidate" for p in plan)
    assert all(p["action"] == "review" for p in plan if memory.get_memory(p["memory_id"])
               and memory.get_memory(p["memory_id"]).get("conflict_status") == "ambiguous_conflict")


def test_unrelated_memories_produce_no_duplicate_or_review_recommendation():
    memory.add_memory("Aku suka gitar")
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    plan = memory.analyze_memory_maintenance()
    assert all(p["action"] == "keep" for p in plan)


# ─────────────────────────────────────────────
# P/Q - obsolete wording, age alone never archives
# ─────────────────────────────────────────────


def test_obsolete_wording_flags_even_a_fresh_entry():
    entry = _entry("untuk sementara pakai VPS test ini buat eksperimen doang", importance=1, days_ago=0)
    memory._memories.append(entry)
    plan = memory.analyze_memory_maintenance()
    assert plan[0]["action"] == "archive"
    assert "obsolete" in plan[0]["reason"]


def test_important_old_memory_never_flagged_obsolete_by_age_alone():
    """A 2-year-old, importance=3 memory with NO obsolete wording must
    NOT be recommended for archive just because it's old - Step 7's own
    'age alone' prohibition, and Step 6's 'a 2-year-old memory can still
    be extremely important' example."""
    entry = _entry("Vinn sedang mengembangkan sistem voice assistant Luno",
                    importance=3, days_ago=730, category="project_context")
    memory._memories.append(entry)
    plan = memory.analyze_memory_maintenance()
    # importance=3, lifecycle stale-at-worst (never archived by age alone
    # at this importance within 730 days per _LIFECYCLE_THRESHOLDS_DAYS),
    # no obsolete wording -> never "archive".
    assert plan[0]["action"] != "archive"


def test_hour_old_memory_can_still_be_flagged_obsolete():
    entry = _entry("lagi coba-coba pakai tool baru buat sekarang", importance=1, days_ago=0)
    memory._memories.append(entry)
    assert memory.compute_lifecycle(entry) == "active"  # fresh by age
    plan = memory.analyze_memory_maintenance()
    assert plan[0]["action"] == "archive"  # but obsolete by wording


# ─────────────────────────────────────────────
# R - current vs historical conflict preserved through maintenance
# ─────────────────────────────────────────────


def test_correction_pair_preserved_current_and_historical_after_maintenance():
    memory.add_memory("Aku pakai RTX 3070 Ti di laptop")
    memory.add_memory("Aku sekarang pakai RTX 3060 Ti di laptop")
    saved = memory.list_memories()
    assert len(saved) == 1  # already merged live by the conflict-resolution pipeline

    plan = memory.analyze_memory_maintenance()
    memory.apply_maintenance_plan(plan)

    after = memory.list_memories()
    assert len(after) == 1
    assert "3060" in after[0]["text"]
    assert any("3070" in h["text"] for h in after[0]["history"])


# ─────────────────────────────────────────────
# S/T/U - planner determinism, dry-run no mutation, execution
# ─────────────────────────────────────────────


def test_planner_is_deterministic_for_the_same_state_and_time():
    memory.add_memory("Aku suka gitar")
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    now = datetime(2026, 6, 1)
    plan1 = memory.analyze_memory_maintenance(now=now)
    plan2 = memory.analyze_memory_maintenance(now=now)
    assert plan1 == plan2


def test_dry_run_preview_never_mutates_state(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "_save", lambda: calls.append(1))
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    calls.clear()  # discard the save from add_memory itself
    before = dict(memory.get_memory(entry["id"]))
    memory.preview_maintenance_text()
    after = dict(memory.get_memory(entry["id"]))
    assert calls == []
    assert before == after


def test_apply_maintenance_plan_actually_mutates_when_appropriate():
    entry = _entry("proyek lama yang jarang dibahas", importance=1, days_ago=30, id_="stale1")
    memory._memories.append(entry)
    plan = memory.analyze_memory_maintenance()
    results = memory.apply_maintenance_plan(plan)
    assert any(r["status"] == "applied" for r in results)
    live = memory.get_memory("stale1")
    assert live["archived_by_maintenance"] is True
    assert memory.compute_lifecycle(live) == "archived"


# ─────────────────────────────────────────────
# V/W - archive preserves data, history preservation on consolidate
# ─────────────────────────────────────────────


def test_archive_preserves_full_entry_data():
    entry = _entry("proyek lama yang jarang dibahas", importance=1, days_ago=30, id_="stale2")
    memory._memories.append(entry)
    results = memory.apply_maintenance_plan(memory.analyze_memory_maintenance())
    assert any(r["memory_id"] == "stale2" and r["status"] == "applied" for r in results)
    live = memory.get_memory("stale2")
    assert live["archived_by_maintenance"] is True
    assert live["text"] == "proyek lama yang jarang dibahas"
    assert live["id"] == "stale2"
    # still fully findable directly, even though archived.
    assert live in memory.list_memories()


def test_consolidate_execution_preserves_loser_text_in_survivor_history():
    a = _entry("GPU laptop pakai RTX 3060 Ti", importance=2, days_ago=1, id_="hc1")
    b = _entry("GPU laptop pakai RTX 3060 Ti", importance=1, days_ago=1, id_="hc2")
    memory._memories.extend([a, b])
    plan = memory.analyze_memory_maintenance()
    memory.apply_maintenance_plan(plan)

    remaining_ids = {m["id"] for m in memory.list_memories()}
    assert "hc1" in remaining_ids
    assert "hc2" not in remaining_ids
    survivor = memory.get_memory("hc1")
    assert any(h["text"] == "GPU laptop pakai RTX 3060 Ti" and h["reason"] == "maintenance_consolidation"
               for h in survivor["history"])


# ─────────────────────────────────────────────
# X/Y - backward-compatible schema, malformed entry safety
# ─────────────────────────────────────────────


def test_schema_v1_entry_without_usage_fields_analyzed_safely():
    memory._memories.append({
        "id": "v1", "text": "user likes tea", "created_at": (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
    })
    plan = memory.analyze_memory_maintenance()
    assert plan[0]["memory_id"] == "v1"
    assert plan[0]["action"] in ("keep", "reinforce", "archive", "consolidate", "review")


def test_malformed_entries_do_not_crash_planner_or_report():
    memory._memories.append("not a dict")
    memory._memories.append({"id": "no-text"})
    memory._memories.append({"id": "bad-conflict", "text": "GPU cocok banget",
                              "conflict_status": "ambiguous_conflict",
                              "conflict_group": {"not": "hashable"}})
    memory._memories.append(_entry("GPU utama RTX 3060 Ti", importance=2, days_ago=1))
    plan = memory.analyze_memory_maintenance()  # must not raise
    report = memory.memory_health_report()  # must not raise
    memory.preview_maintenance_text()  # must not raise
    memory.apply_maintenance_plan(plan)  # must not raise
    assert report["total"] >= 1


def test_malformed_retrieval_count_defaults_safely():
    entry = _entry("weird entry", importance=1, days_ago=1, retrieval_count="not a number")
    memory._memories.append(entry)
    plan = memory.analyze_memory_maintenance()  # must not raise
    assert plan[0]["memory_id"] == entry["id"]


# ─────────────────────────────────────────────
# Z - persistence across restart
# ─────────────────────────────────────────────


def test_usage_and_archive_metadata_survive_restart():
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    memory.record_memory_usage([_rm(entry)])
    stale_entry_id = memory.add_memory("proyek lama yang jarang dibahas dulu sekali")["id"]
    # force the second entry old + low importance directly (bypassing add_memory's own dating)
    for m in memory._memories:
        if m["id"] == stale_entry_id:
            old_ts = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
            m["created_at"] = m["updated_at"] = old_ts
            m["importance"] = 1
    memory._save()  # persist the manual mutation before maintenance runs
    memory.apply_maintenance_plan(memory.analyze_memory_maintenance())

    before = [dict(m) for m in memory.list_memories()]
    memory._memories = []
    memory._load()
    after = [dict(m) for m in memory.list_memories()]
    assert after == before
    reloaded_entry = memory.get_memory(entry["id"])
    assert reloaded_entry["retrieval_count"] == 1


# ─────────────────────────────────────────────
# AA - health report
# ─────────────────────────────────────────────


def test_health_report_counts_are_consistent():
    memory.add_memory("ini penting banget, aku alergi kacang")  # importance 4
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")  # importance 2
    memory.add_memory("Aku pakai Windows 11")
    memory.add_memory("Aku pakai Ubuntu")  # ambiguous conflict with the above

    report = memory.memory_health_report()
    assert report["total"] == 4
    assert report["protected_core_memories"] >= 1  # the importance=4 one
    assert report["potential_conflicts"] == 1
    assert sum(report["importance"].values()) == report["total"]
    assert sum(report["lifecycle"].values()) == report["total"]

    text = memory.format_memory_health_report(report)
    assert "Memory Health" in text
    assert "Total: 4" in text


def test_health_report_is_read_only(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "_save", lambda: calls.append(1))
    memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    calls.clear()
    memory.memory_health_report()
    assert calls == []


# ─────────────────────────────────────────────
# AB/AC - manual command detection, ordinary conversation no side effect
# ─────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "cek kesehatan memory",
    "cek kesehatan memory.",
    "tolong cek kesehatan memory",
    "memory health",
])
def test_detect_memory_health_command_matches(text):
    assert memory.detect_memory_health_command(text)


@pytest.mark.parametrize("text", [
    "analisa memory",
    "cek memory yang sudah basi",
    "preview maintenance memory",
])
def test_detect_memory_maintenance_preview_command_matches(text):
    assert memory.detect_memory_maintenance_preview_command(text)


@pytest.mark.parametrize("text", [
    "rapikan memory",
    "jalankan maintenance memory",
])
def test_detect_memory_maintenance_run_command_matches(text):
    assert memory.detect_memory_maintenance_run_command(text)


def test_detect_archive_memory_by_id_command_captures_id():
    assert memory.detect_archive_memory_by_id_command("arsipkan memory nomor 12") == "12"


def test_detect_unarchive_last_memory_command_matches():
    assert memory.detect_unarchive_last_memory_command("jangan arsipkan memory ini")


@pytest.mark.parametrize("text", [
    "aku suka kopi item",
    "gimana cuaca hari ini",
    "matikan lampu kamar",
    "aku lagi ngoding fitur baru",
])
def test_ordinary_conversation_never_matches_any_maintenance_command(text):
    assert not memory.detect_memory_health_command(text)
    assert not memory.detect_memory_maintenance_preview_command(text)
    assert not memory.detect_memory_maintenance_run_command(text)
    assert memory.detect_archive_memory_by_id_command(text) is None
    assert not memory.detect_unarchive_last_memory_command(text)


def test_ordinary_conversation_never_triggers_maintenance_mutation(monkeypatch):
    """Simulates what `_handle_explicit_memory_command` does: an ordinary
    utterance never reaches `apply_maintenance_plan()`/
    `archive_memory_by_id()` at all, because none of the detectors match."""
    calls = []
    monkeypatch.setattr(memory, "apply_maintenance_plan", lambda plan: calls.append(plan) or [])
    monkeypatch.setattr(memory, "archive_memory_by_id", lambda mid: calls.append(mid) or ("not_found", None))
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")

    text = "berapa suhu CPU ideal?"
    if memory.detect_memory_maintenance_run_command(text):
        memory.apply_maintenance_plan(memory.analyze_memory_maintenance())
    archive_id = memory.detect_archive_memory_by_id_command(text)
    if archive_id:
        memory.archive_memory_by_id(archive_id)

    assert calls == []
    assert memory.get_memory(entry["id"]) == entry


# ─────────────────────────────────────────────
# AD - protected memory cannot be automatically archived even via a
# malformed/incorrect plan entry
# ─────────────────────────────────────────────


def test_protected_entry_refuses_archive_even_if_plan_says_so():
    entry = _entry("core memory", importance=4, days_ago=1, id_="protected1")
    memory._memories.append(entry)
    fake_plan = [{"memory_id": "protected1", "action": "archive", "reason": "malformed plan", "confidence": 1.0}]
    results = memory.apply_maintenance_plan(fake_plan)
    assert results[0]["status"] == "blocked_protected"
    live = memory.get_memory("protected1")
    assert not live.get("archived_by_maintenance")
    assert memory.compute_lifecycle(live) != "archived"


def test_archive_memory_by_id_refuses_protected_memory():
    entry = _entry("core memory", importance=4, days_ago=1, id_="protected2")
    memory._memories.append(entry)
    status, returned = memory.archive_memory_by_id("protected2")
    assert status == "protected"
    live = memory.get_memory("protected2")
    assert not live.get("archived_by_maintenance")


# ─────────────────────────────────────────────
# AE - bounded maintenance (analysis is explicit-only, not run on
# ordinary retrieval)
# ─────────────────────────────────────────────


def test_ordinary_retrieval_never_calls_the_planner(monkeypatch):
    calls = []
    monkeypatch.setattr(memory, "analyze_memory_maintenance", lambda now=None: calls.append(1) or [])
    entry = memory.add_memory("PC utamaku pakai RTX 3060 Ti")
    # Ordinary usage-tracking path (what happens every real turn) - must
    # never touch the planner.
    memory.record_memory_usage([_rm(entry)])
    assert calls == []
