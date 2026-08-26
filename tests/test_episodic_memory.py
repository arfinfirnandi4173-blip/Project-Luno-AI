"""
LUNO Shared Experience & Episodic Memory Layer sprint - test suite for
`luno/episodic_memory.py`.

Same flat `tests/test_<module>.py` convention as `tests/test_relationship_engine.py`/
`tests/test_emotion_engine.py` for a brand-new, self-contained, top-level
loose module (see `ARCHITECTURE_GUARD.md` §6/§10). One end-to-end
integration test lives separately in `tests/test_runtime_demo.py`
(`test_episodic_memory_end_to_end_detect_persist_retrieve_alongside_existing_context`).

Categories (mirrors this sprint's own §20-ish test requirements):
  - Detection / Grounding (meaningful vs. NOT meaningful, bilingual,
    negation-aware, never triggered by tool success alone)
  - Persistence (missing/empty/malformed/wrong-root-type/partial-entry files)
  - Deduplication (same event twice, same summary twice, same event
    after a simulated restart)
  - Retrieval (relevant query surfaces it, irrelevant/technical query does
    not, bounded via has_any_signal, integrates with the EXISTING
    memory_retrieval bounding/temporal-wording machinery)
  - Temporal behavior (freshness wording reused from the retriever, not
    reinvented here)
  - Relationship integration (one-way: observe_turn()'s boolean can drive
    RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE; a duplicate/no-candidate
    turn never fabricates that signal)
  - Isolation (never imports luno.memory/emotion_engine/persona/relationship_engine)
  - Determinism (same input -> same experience_id / same result)
  - Bounded growth (oldest dropped once EPISODIC_MEMORY_MAX_ENTRIES exceeded)
"""

import json
import math

import pytest

from luno.episodic_memory import (
    EPISODIC_SCHEMA_VERSION,
    EpisodicExperience,
    EpisodicMemoryStore,
    ExperienceCategory,
    detect_candidate_experience,
    make_episodic_experience_source,
    observe_turn,
)
from luno.memory_retrieval import MemoryRetrievalConfig, MemoryRetriever
from luno.memory_retrieval.query import analyze_query


# ─────────────────────────────────────────────
#  Detection / Grounding
# ─────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "berapa 1+1?",
    "halo",
    "nyalakan lampu",
    "nyalain lampu kamar",
    "berapa suhu cpu sekarang?",
    "apa arti kata algoritma?",
    "matiin AC",
    "kenapa docker error?",
    "cara setting esp32?",
    "berapa tegangan resistornya?",
    "udah selesai makan siang",  # outcome word present, but no topic/collab anchor
])
def test_ordinary_turns_never_detected_as_experiences(text):
    assert detect_candidate_experience(text, had_successful_tool_call=True) is None


def test_negated_outcome_is_not_detected():
    """"masalahnya belum kelar" (not yet resolved) must NOT be mistaken for
    a resolved problem - section 8's "never invent" applies just as much to
    inverting a stated negative into a false positive."""
    assert detect_candidate_experience("masalahnya belum kelar juga", had_successful_tool_call=True) is None
    assert detect_candidate_experience("the bug is not fixed yet") is None


def test_tool_success_alone_never_creates_an_experience():
    """A bare device command with a verified successful tool call (turning
    on a light) must never, by itself, be treated as meaningful - only an
    explicit textual signal can gate detection (see module docstring's
    "WHAT COUNTS AS AN EXPERIENCE")."""
    for text in ("nyalakan lampu", "turn on the lights", "matikan AC", "set brightness to 50"):
        assert detect_candidate_experience(text, had_successful_tool_call=True) is None


def test_technical_problem_solved_detected_bilingual():
    c1 = detect_candidate_experience("akhirnya masalah dockernya kelar juga setelah 3 jam", had_successful_tool_call=True)
    assert c1 is not None
    assert c1.category == ExperienceCategory.TECHNICAL_PROBLEM_SOLVED
    assert "tool_verified" in c1.source

    c2 = detect_candidate_experience("we finally fixed that annoying wifi bug")
    assert c2 is not None
    assert c2.category == ExperienceCategory.TECHNICAL_PROBLEM_SOLVED
    assert "tool_verified" not in c2.source


def test_device_configured_detected_bilingual():
    c1 = detect_candidate_experience("akhirnya berhasil setting ESP32 nyambung ke home assistant", had_successful_tool_call=True)
    assert c1 is not None
    assert c1.category == ExperienceCategory.DEVICE_CONFIGURED

    c2 = detect_candidate_experience("we finally configured a new sensor for home assistant")
    assert c2 is not None
    assert c2.category == ExperienceCategory.DEVICE_CONFIGURED


def test_milestone_detected_requires_topic_or_collaboration_cue():
    c1 = detect_candidate_experience("we finally got the whole project milestone done")
    assert c1 is not None
    assert c1.category == ExperienceCategory.MILESTONE

    c2 = detect_candidate_experience("akhirnya kelar juga project kita berdua")
    assert c2 is not None
    assert c2.category == ExperienceCategory.MILESTONE

    # "udah selesai" with NEITHER a topic anchor NOR a collaboration
    # pronoun ("kita"/"we"/"together") is deliberately excluded - see
    # test_ordinary_turns_never_detected_as_experiences's "makan siang" case.


def test_meaningful_moment_detected_without_requiring_outcome_word():
    c = detect_candidate_experience("ini momen penting banget buat aku")
    assert c is not None
    assert c.category == ExperienceCategory.MEANINGFUL_MOMENT

    c2 = detect_candidate_experience("this was a big moment for me")
    assert c2 is not None
    assert c2.category == ExperienceCategory.MEANINGFUL_MOMENT


def test_explicit_memory_shared_flag_recorded_in_provenance():
    c = detect_candidate_experience(
        "akhirnya masalah dockernya kelar juga", had_successful_tool_call=True, explicit_memory_shared=True,
    )
    assert c is not None
    assert "tool_verified" in c.source
    assert "explicit_user_statement" in c.source


def test_grounding_summary_is_a_literal_slice_of_real_text_never_invented():
    """The stored summary must be traceable back to the actual turn text -
    never LLM paraphrase (there is no LLM call anywhere in this module)."""
    text = "akhirnya masalah dockernya kelar juga setelah 3 jam"
    c = detect_candidate_experience(text)
    assert c.raw_summary == text.strip()


def test_none_and_empty_and_non_string_text_never_raise():
    assert detect_candidate_experience(None) is None
    assert detect_candidate_experience("") is None
    assert detect_candidate_experience("   ") is None
    assert detect_candidate_experience(12345) is None  # type: ignore[arg-type]


# ─────────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────────


def test_missing_file_loads_empty_list(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "does_not_exist.json"))
    assert EpisodicMemoryStore.load() == []


def test_empty_file_loads_empty_list(tmp_path, monkeypatch):
    from luno import config as config_module

    path = tmp_path / "episodic_memory.json"
    path.write_text("")
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(path))
    assert EpisodicMemoryStore.load() == []


def test_malformed_json_loads_empty_list_never_raises(tmp_path, monkeypatch):
    from luno import config as config_module

    path = tmp_path / "episodic_memory.json"
    path.write_text("not valid json {{{")
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(path))
    assert EpisodicMemoryStore.load() == []


@pytest.mark.parametrize("bad_root", ['{"not": "a list"}', '"just a string"', "42", "null", "true"])
def test_wrong_root_type_loads_empty_list(tmp_path, monkeypatch, bad_root):
    from luno import config as config_module

    path = tmp_path / "episodic_memory.json"
    path.write_text(bad_root)
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(path))
    assert EpisodicMemoryStore.load() == []


def test_partial_and_malformed_entries_are_individually_skipped(tmp_path, monkeypatch):
    from luno import config as config_module

    good = {
        "schema_version": EPISODIC_SCHEMA_VERSION, "experience_id": "abc123",
        "timestamp": 100.0, "category": "milestone", "summary": "a real summary",
        "source": "conversation",
    }
    bad_entries = [
        {"experience_id": "missing_other_fields"},
        "not a dict at all",
        {**good, "schema_version": 999},  # wrong schema version
        {**good, "experience_id": ""},  # empty id
        {**good, "timestamp": "not a number"},
        {**good, "timestamp": float("nan")},
        {**good, "timestamp": float("inf")},
        {**good, "timestamp": -5.0},
        {**good, "category": "not_a_real_category"},
        {**good, "summary": ""},
        {**good, "summary": 12345},
        None,
        123,
        [],
    ]
    data = [good] + bad_entries
    path = tmp_path / "episodic_memory.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(path))

    loaded = EpisodicMemoryStore.load()
    assert len(loaded) == 1
    assert loaded[0].experience_id == "abc123"


def test_missing_source_field_defaults_safely(tmp_path, monkeypatch):
    from luno import config as config_module

    entry = {
        "schema_version": EPISODIC_SCHEMA_VERSION, "experience_id": "xyz",
        "timestamp": 1.0, "category": "milestone", "summary": "test", "source": "",
    }
    path = tmp_path / "episodic_memory.json"
    path.write_text(json.dumps([entry]))
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(path))
    loaded = EpisodicMemoryStore.load()
    assert len(loaded) == 1
    assert loaded[0].source == "conversation"


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    entry = EpisodicExperience(
        experience_id="fp1", timestamp=123.0, category=ExperienceCategory.MILESTONE.value,
        summary="we finally finished the project", source="conversation",
    )
    assert EpisodicMemoryStore.save([entry]) is True
    loaded = EpisodicMemoryStore.load()
    assert len(loaded) == 1
    assert loaded[0] == entry


def test_save_is_atomic_no_partial_file_left_on_interrupted_write(tmp_path, monkeypatch):
    """Same tmp-then-os.replace() convention as RelationshipStore.save -
    verifies the .tmp file never lingers after a successful save."""
    from luno import config as config_module

    target = tmp_path / "episodic_memory.json"
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(target))
    EpisodicMemoryStore.save([EpisodicExperience(experience_id="a", timestamp=1.0, summary="x")])
    assert target.exists()
    assert not (tmp_path / "episodic_memory.json.tmp").exists()


def test_save_with_no_configured_path_returns_false(monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", "")
    assert EpisodicMemoryStore.save([]) is False


# ─────────────────────────────────────────────
#  observe_turn() - detect -> validate -> deduplicate -> persist
# ─────────────────────────────────────────────


def test_observe_turn_no_candidate_returns_false_none(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    is_new, entry = observe_turn("berapa suhu cpu sekarang?", had_successful_tool_call=True)
    assert is_new is False
    assert entry is None
    assert EpisodicMemoryStore.load() == []


def test_observe_turn_new_candidate_persists(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    is_new, entry = observe_turn("akhirnya masalah dockernya kelar juga", had_successful_tool_call=True, now=1000.0)
    assert is_new is True
    assert entry is not None
    assert entry.timestamp == 1000.0
    assert len(EpisodicMemoryStore.load()) == 1


def test_observe_turn_same_event_twice_deduplicates(tmp_path, monkeypatch):
    """Same event/summary processed twice in the same "session" (no reload
    in between) must not create a second stored record."""
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    text = "akhirnya masalah dockernya kelar juga"
    is_new1, entry1 = observe_turn(text, had_successful_tool_call=True, now=1000.0)
    is_new2, entry2 = observe_turn(text, had_successful_tool_call=True, now=2000.0)
    assert is_new1 is True
    assert is_new2 is False
    assert entry1.experience_id == entry2.experience_id
    assert len(EpisodicMemoryStore.load()) == 1


def test_observe_turn_same_summary_twice_deduplicates_even_with_different_provenance(tmp_path, monkeypatch):
    """Section 13: "Do not blindly use timestamp-based uniqueness" - the
    SAME summary text, even reported with different corroborating signals
    (tool_verified vs. not), is still the same real-world event and must
    collapse to one stored record."""
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    text = "akhirnya masalah dockernya kelar juga"
    observe_turn(text, had_successful_tool_call=True, now=1000.0)
    is_new2, _ = observe_turn(text, had_successful_tool_call=False, now=2000.0)
    assert is_new2 is False
    assert len(EpisodicMemoryStore.load()) == 1


def test_observe_turn_same_event_after_simulated_restart_still_deduplicates(tmp_path, monkeypatch):
    """Content-fingerprint-based `experience_id` is restart-safe: dedup
    must work purely from what's on disk, with no in-memory state carried
    between calls (`observe_turn` itself never caches anything - every
    call freshly reloads via `EpisodicMemoryStore.load()`), simulating a
    process restart between the two observations."""
    from luno import config as config_module

    path = tmp_path / "episodic_memory.json"
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(path))
    text = "we finally fixed that annoying wifi bug"
    observe_turn(text, now=1000.0)
    # Nothing kept in-process; a second call is indistinguishable from a
    # fresh process reading the same file from disk.
    is_new2, entry2 = observe_turn(text, now=5000.0)
    assert is_new2 is False
    assert entry2.timestamp == 1000.0  # the ORIGINAL entry, never overwritten
    assert len(EpisodicMemoryStore.load()) == 1


def test_observe_turn_slightly_different_wording_is_a_distinct_event(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    observe_turn("akhirnya masalah dockernya kelar juga", now=1000.0)
    observe_turn("akhirnya berhasil setting ESP32 nyambung ke home assistant", now=2000.0)
    assert len(EpisodicMemoryStore.load()) == 2


def test_bounded_growth_drops_oldest_when_over_max_entries(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_MAX_ENTRIES", 3)

    for i in range(5):
        observe_turn(f"we finally fixed bug number {i}", now=float(i))

    stored = EpisodicMemoryStore.load()
    assert len(stored) == 3
    # Oldest (bug 0, bug 1) dropped - newest 3 remain.
    summaries = [e.summary for e in stored]
    assert "bug number 0" not in " ".join(summaries)
    assert "bug number 1" not in " ".join(summaries)
    assert "bug number 4" in " ".join(summaries)


def test_observe_turn_never_raises_on_bad_input():
    is_new, entry = observe_turn(None)
    assert (is_new, entry) == (False, None)
    is_new, entry = observe_turn(12345)  # type: ignore[arg-type]
    assert (is_new, entry) == (False, None)


# ─────────────────────────────────────────────
#  Determinism
# ─────────────────────────────────────────────


def test_same_text_always_produces_the_same_experience_id():
    text = "akhirnya masalah dockernya kelar juga"
    c1 = detect_candidate_experience(text)
    c2 = detect_candidate_experience(text)
    from luno.episodic_memory import _build_experience

    e1 = _build_experience(c1, now=1.0)
    e2 = _build_experience(c2, now=999.0)  # different `now` - id must not depend on it
    assert e1.experience_id == e2.experience_id


def test_different_category_same_text_would_differ_but_detection_is_stable():
    """`detect_candidate_experience` itself is a pure function - identical
    input always yields an identical category/summary/source triple."""
    text = "we finally configured a new sensor for home assistant"
    results = {detect_candidate_experience(text) for _ in range(5)}
    assert len(results) == 1


# ─────────────────────────────────────────────
#  Retrieval
# ─────────────────────────────────────────────


def _retriever_with_episodic_source(experiences):
    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("episodic_memory", make_episodic_experience_source(lambda: experiences))
    return retriever


def test_retrieval_relevant_query_surfaces_the_experience():
    exp = EpisodicExperience(
        experience_id="fp1", timestamp=None, category=ExperienceCategory.TECHNICAL_PROBLEM_SOLVED.value,
        summary="akhirnya masalah docker kelar juga", source="conversation",
    )
    retriever = _retriever_with_episodic_source([exp])
    results = retriever.retrieve_memories("kemarin kita benerin docker apa ya?")
    assert len(results) == 1
    assert "Shared experience with the user" in results[0].text
    assert "docker" in results[0].text.lower()


def test_retrieval_irrelevant_technical_query_does_not_dump_history():
    """Section 11's own worked example: "berapa suhu CPU?" must NOT
    surface unrelated shared-experience history, even when one exists."""
    exp = EpisodicExperience(
        experience_id="fp1", timestamp=None, category=ExperienceCategory.TECHNICAL_PROBLEM_SOLVED.value,
        summary="akhirnya masalah docker kelar juga", source="conversation",
    )
    retriever = _retriever_with_episodic_source([exp])
    results = retriever.retrieve_memories("berapa suhu cpu sekarang?")
    assert results == []


def test_retrieval_no_signal_query_never_touches_the_provider():
    calls = []

    def _provider():
        calls.append(1)
        return []

    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("episodic_memory", make_episodic_experience_source(_provider))
    retriever.retrieve_memories("what's 5 + 5?")
    assert calls == []  # has_any_signal is False - provider never even called


def test_retrieval_provider_failure_returns_empty_never_raises():
    def _broken_provider():
        raise RuntimeError("disk on fire")

    retriever = MemoryRetriever(MemoryRetrievalConfig())
    retriever.register_source("episodic_memory", make_episodic_experience_source(_broken_provider))
    results = retriever.retrieve_memories("kemarin kita benerin apa ya?")
    assert results == []


def test_retrieval_empty_experience_list_returns_empty():
    retriever = _retriever_with_episodic_source([])
    results = retriever.retrieve_memories("kemarin kita benerin apa ya?")
    assert results == []


def test_retrieval_time_reference_query_gets_a_score_bonus():
    exp = EpisodicExperience(
        experience_id="fp1", timestamp=None, category=ExperienceCategory.MILESTONE.value,
        summary="we finally finished the project milestone", source="conversation",
    )
    source = make_episodic_experience_source(lambda: [exp])

    query_with_time = analyze_query("yesterday what project milestone did we finish?")
    query_without_time = analyze_query("what project milestone did we finish?")

    r_with = source(query_with_time, MemoryRetrievalConfig())
    r_without = source(query_without_time, MemoryRetrievalConfig())
    assert r_with[0].score > r_without[0].score


def test_retrieval_dot_and_raw_id_present_for_retriever_dedup_compat():
    """`MemoryRetriever._deduplicate()` keys on `getattr(mem.raw, "id", None)`
    - `EpisodicExperience.id` must alias `experience_id` so retrieval-time
    dedup (a different concern from storage-time dedup) works the same
    way it does for every other source's `raw` object."""
    exp = EpisodicExperience(experience_id="fp1", timestamp=None, summary="test summary", category=ExperienceCategory.MILESTONE.value)
    assert exp.id == "fp1" == exp.experience_id


# ─────────────────────────────────────────────
#  Relationship integration (one-way: Episodic -> Relationship, never reverse)
# ─────────────────────────────────────────────


def test_new_experience_signal_can_drive_relationship_meaningful_shared_experience(tmp_path, monkeypatch):
    from luno import config as config_module
    from luno.relationship_engine import RelationshipSignal, classify_turn

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    is_new, _ = observe_turn("akhirnya masalah dockernya kelar juga", now=1.0)
    assert is_new is True

    signals = classify_turn("akhirnya masalah dockernya kelar juga", explicit_memory_shared=is_new)
    assert RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE in signals


def test_duplicate_experience_never_fabricates_a_relationship_signal(tmp_path, monkeypatch):
    """"Relationship Engine must never create fake memories simply to
    increase closeness. A shared experience should only influence
    relationship state if the actual experience exists" - a duplicate
    (already-known) event must not re-trigger the signal a second time."""
    from luno import config as config_module
    from luno.relationship_engine import RelationshipSignal, classify_turn

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    text = "akhirnya masalah dockernya kelar juga"
    observe_turn(text, now=1.0)
    is_new2, _ = observe_turn(text, now=2.0)
    assert is_new2 is False

    signals = classify_turn(text, explicit_memory_shared=is_new2)
    assert RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE not in signals


def test_ordinary_turn_never_fabricates_a_relationship_signal(tmp_path, monkeypatch):
    from luno import config as config_module
    from luno.relationship_engine import RelationshipSignal, classify_turn

    monkeypatch.setattr(config_module, "EPISODIC_MEMORY_FILE", str(tmp_path / "episodic_memory.json"))
    is_new, _ = observe_turn("nyalakan lampu", had_successful_tool_call=True)
    assert is_new is False

    signals = classify_turn("nyalakan lampu", explicit_memory_shared=is_new)
    assert RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE not in signals


# ─────────────────────────────────────────────
#  Isolation
# ─────────────────────────────────────────────


def test_episodic_memory_module_never_imports_memory_emotion_persona_relationship():
    """Uses `ast` to inspect actual `import`/`from ... import` statements
    only - not a raw substring search over the whole file, which would
    trivially self-match this module's own docstring (it explicitly NAMES
    `luno.memory`/`luno.emotion_engine`/`luno.persona`/
    `luno.relationship_engine` in prose, explaining what it does NOT
    import). `luno.memory_retrieval*` is an explicitly allowed exception -
    read-only reuse of the retrieval package's types/helpers, one-way,
    never imported back by that package."""
    import ast
    import inspect
    import luno.episodic_memory as em_module

    source = inspect.getsource(em_module)
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)

    allowed_prefixes = ("memory_retrieval", ".memory_retrieval")
    forbidden_prefixes = ("memory", "memory_guard", "emotion_engine", "persona", "relationship_engine")

    for name in imported:
        if any(name.startswith(p) for p in allowed_prefixes):
            continue
        for forbidden in forbidden_prefixes:
            assert not (name == forbidden or name.startswith(forbidden + ".")), (
                f"unexpected import touching {forbidden!r}: {name!r}"
            )


def test_detection_never_touches_relationship_engine_module(monkeypatch):
    """Belt-and-suspenders runtime check alongside the static AST check
    above: patch `luno.relationship_engine` so any attribute access would
    raise, and prove ordinary detection/persistence calls never trigger it."""
    import luno.relationship_engine as re_module

    class _Trap:
        def __getattr__(self, name):
            raise AssertionError(f"episodic_memory unexpectedly touched relationship_engine.{name}")

    monkeypatch.setattr(re_module, "RelationshipEngine", _Trap())
    detect_candidate_experience("akhirnya masalah dockernya kelar juga", had_successful_tool_call=True)
