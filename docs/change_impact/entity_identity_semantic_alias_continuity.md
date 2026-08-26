# SPRINT 45 — ENTITY IDENTITY & SEMANTIC ALIAS CONTINUITY

## STATUS

Complete. Two genuine gaps found and fixed via five small, additive
edits across two files. Everything else the sprint's own brief asked
about was verified, via live `RuntimeDemoConsole` reproduction, to
already be correctly handled by Sprint 43/44's existing machinery and
was deliberately left untouched.

## ROOT CAUSE

Phase 0 (read-only reconnaissance) confirmed the existing architecture
already has a bounded, deterministic alias-normalization layer (Sprint
43's `_TOKEN_SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES`/`_strip_bounded_
affixes()`) and ambiguity-safety guards (Sprint 44's low-ambiguity
single-token fallback and its multi-topic refusal extension). Phase 1
built a comprehensive live probe matrix through the real
`RuntimeDemoConsole` covering: verb alias ("ganti"->"upgrade" GPU),
action alias ("beli"->"ganti" via a shared noun), device alias/
abbreviation ("mikrokontroler"->ESP32, "ESP32-S3"->"S3"), audio alias
("mikrofon"->"mic"), lighting ("LED strip"->"lampu LED"), aquascape
alias ("pompa"->"water pump"), a false-positive control, 2/3/5-topic
ambiguity, word-shape traps, Indonesian morphology, corrections, a new-
entity boundary case, an unrelated-topic control, cross-conversation
isolation, and a long alias chain.

**Finding: nearly everything already worked.** Every alias/abbreviation/
correction/multi-topic scenario resolved correctly on the first probe
run, via mechanisms already in place: raw token overlap (exact
identity), the existing synonym groups (alias), natural hyphen-
tokenization splitting "ESP32-S3" into "esp32"+"s3" tokens (abbreviation
- no code needed), and Sprint 44's ambiguity/multi-topic guards
(correctly refusing to guess). Per this sprint's explicit instruction,
none of these were modified.

**Two real, narrow gaps were found**, both root-caused to the FIRST
stage where continuity broke - classification, not retrieval, ranking,
or budget:

1. **"gimana"/"bagaimana" register asymmetry.** "gimana" is simply the
   colloquial contraction of the standard Indonesian question word
   "bagaimana" ("how") - the SAME lexical item, not a different one.
   This equivalence was already assumed elsewhere in the codebase (a
   general question-marker regex near the top of `luno/memory.py`
   already lists both; `_ATTRIBUTE_RESIDUAL_STOPWORDS` already lists
   both; `_COMPARISON_PRESERVATION_EXTRA_FILLER` - a Sprint-era addition
   for a DIFFERENT decision - already lists "bagaimana" specifically
   because of this same equivalence). Live reproduction (Scenario G: a
   correction from "ESP32 pakai INMP441." to "Eh maksudku ESP32-S3."
   followed by "Mic-nya bagaimana?") found the corrected, current topic
   was available, but the query fell all the way to `classify_
   reference_type() == "unknown"` and injected nothing, purely because
   the user used the formal register. Root cause traced to FOUR separate
   spots that had each independently missed "bagaimana" where "gimana"
   was already present, plus a fifth found by this sprint's own test
   suite before being called done (see FIX below).

2. **3-letter acronym + "-nya" clitic.** `_MIN_AFFIX_ROOT_LEN=4` (Sprint
   43's guard against corrupting short product identifiers via prefix/
   derivational-suffix stripping) also blocked stripping the "-nya"
   possessive clitic from any 3-letter root, so a fused (no hyphen)
   "SSDnya"/"CPUnya"/"PSUnya" never normalized to "ssd"/"cpu"/"psu" at
   all. Live reproduction (an SSD topic established, then superseded by
   a more recent GPU topic, then "SSDnya gimana?") found this was a real
   ambiguity-safety failure: the query wrongly attached to the GPU topic
   via Sprint 44's "recency when unopposed" fallback, since the SSD
   topic was never even considered a candidate (neither raw nor
   normalized overlap could see past the un-stripped "ssdnya" token).

Both gaps were confirmed, via direct code reading, to originate in the
classification/normalization stage - well before retrieval, ranking, or
budget are ever consulted. Neither `_rank_key()`, `_apply_budget()`, nor
`assemble_context()`'s own logic was found responsible for anything, and
none of the three was modified.

## BEFORE

"Mic-nya bagaimana?" after a correction to ESP32-S3 fell to `"unknown"`
and injected nothing, even though the identical question phrased as
"Mic-nya gimana?" already worked. A fused "SSDnya gimana?" asked after a
more recent, unrelated GPU topic wrongly attached to the GPU topic
instead of the SSD topic the word plainly names, since "ssdnya" could
never normalize to "ssd" at all.

## FIX

Five small, additive, single-purpose edits, all traceable to the two
root causes above:

1. `luno/memory._COMPARISON_MARKER_RE` - added `\bbagaimana\b` as an
   alternative alongside the existing `\bgimana\b`.
2. `luno/memory.classify_reference_type()` - its own inline comparison-
   branch residual filter (`words = [... w != "gimana"]`) extended to
   also exclude `"bagaimana"`. Found necessary by this sprint's OWN unit
   test (`test_04`): without it, a bare "Bagaimana?" was misclassified
   `comparison` instead of `direct_reference`, an asymmetry a bare
   "Gimana?" never had - the marker-regex fix alone was incomplete.
3. `luno/memory._attribute_reference_word()` - its candidate-word
   exclusion check (`word == "gimana"`) widened to `word in ("gimana",
   "bagaimana")`, so a "Yang bagaimana?" fragment is correctly excluded
   the same way "Yang gimana?" already was.
4. `luno/memory_context._TOPIC_OVERLAP_STOPWORDS` - added `"bagaimana"`
   alongside the existing `"gimana"` entry, so once correctly
   classified, the word does not count as a second "real" token and
   defeat Sprint 44's low-ambiguity single-token fallback.
5. `luno/memory_context._strip_bounded_affixes()` - a new, narrower
   `_MIN_CLITIC_ROOT_LEN=3` constant, used ONLY by the "-nya" clitic-
   suffix stripping pass (never the derivational-suffix or prefix
   passes, which keep the original `_MIN_AFFIX_ROOT_LEN=4`). "-nya" is a
   single, closed-class, unambiguous possessive marker - unlike general
   prefix/derivational stripping, there is no realistic 3-letter
   Indonesian or technical root this could plausibly corrupt (verified:
   no member of `_TOKEN_SYNONYM_CANON` or any test fixture's vocabulary
   is itself a real word ending in "nya" at exactly 6 total characters).

## AFTER

"Mic-nya bagaimana?" now classifies and resolves identically to "Mic-nya
gimana?" in every tested context, including after a correction. "SSDnya
gimana?" now correctly resolves to the SSD topic specifically, even with
a more recent, unrelated GPU topic present - verified via a dedicated
multi-topic E2E test, not merely a single-topic coincidence.

## ENTITY MODEL

No new entity/concept representation was introduced. Confirmed via
direct investigation that the existing flat bag-of-terms (`Active
TopicSnapshot`, unchanged since Sprint 40) combined with raw/normalized
token overlap already correctly distinguishes:

- **Exact identity** - raw token match (e.g. "ESP32" mentioned again
  verbatim).
- **Alias** - Sprint 43's existing synonym groups (mic/mikrofon/
  microphone; pompa/pump; ganti/upgrade; gpu/vga; board/mikrokontroler).
- **Abbreviation** - falls out naturally from hyphen-based tokenization
  ("ESP32-S3" tokenizes as `["esp32", "s3"]`), no code needed; a bare
  "S3" with no prior "ESP32-S3" mention correctly matches nothing
  (`test_28`).
- **Subtype/parent/attribute relationships requiring world knowledge**
  - correctly and deliberately NOT fabricated. The system never treats
  a specific product name ("INMP441") as automatically belonging to a
  generic category ("mic") unless the category word was actually used
  somewhere in the conversation (`test_51`, `test_85`). This is not a
  gap; it is the correct, conservative boundary the brief itself asked
  for ("do not assume every pair is automatically equivalent").
- **Merely contextual association** - also correctly NOT treated as an
  alias. "gaming"/"performa" commonly co-occur with GPU discussions but
  are not lexical aliases for "gpu"; a query using them with no other
  evidence is correctly refused (`test_52`).

## ALIAS MODEL

Unchanged from Sprint 43. Zero new synonym groups or phrase-table
entries were added this sprint (`test_42`, `test_43` lock this in as a
structural invariant). The only additions were to CLASSIFICATION-STAGE
word lists (comparison-marker regex, stopword list, attribute-exclusion
check) recognizing that "gimana" and "bagaimana" are the same word, and
a narrower root-length floor for a single, well-understood grammatical
clitic - neither is a new alias/synonym mechanism.

## AMBIGUITY POLICY

Unchanged, re-verified more thoroughly than before. A single novel word
is refused whenever 2+ genuinely distinct topics are live in the bounded
history (re-verified at 2, 3, and 5 competing topics). A word shared
literally by two topics surfaces both candidates rather than guessing
one (pre-existing `select_topic_candidates()` raw-overlap-tie behavior).
A richer query using words that are merely contextually associated with
a topic, but not lexically aliased to it, is refused rather than
guessed. None of the fixes in this sprint touch the ambiguity-refusal
logic itself - they only affect whether a turn is classified with a
comparison marker at all, and whether a fused clitic normalizes to its
root; the refusal machinery downstream is completely unchanged and was
exercised, unmodified, by every test in Section 6-7 of the new suite.

## FALSE-POSITIVE SAFETY

Extensively verified at the token-boundary level: "microscope" never
canonicalizes to "mic"; "pumpa" (not a real word) never canonicalizes to
"pompa"; generic "lampu"/"lamp" never auto-collapse into "led" (an
explicit brief requirement); "GPU" and "upgrade GPU" remain distinct
tokens; "gaming" is never treated as a GPU alias. The `_MIN_CLITIC_ROOT_
LEN=3` widening was scoped as narrowly as possible (only the "-nya"
pass) specifically to avoid reopening the exact class of false positive
`_MIN_AFFIX_ROOT_LEN=4` was built to prevent.

## E2E RESULTS

17 tests through real `RuntimeDemoConsole`, all passing: verb alias,
action alias, device abbreviation, audio alias, aquascape alias, false-
positive control, 2-topic ambiguity refusal, correction + formal-
register attribute followup (the sprint's primary fix), new-entity
conservative refusal, unrelated-topic zero-injection after a detailed
chain, cross-conversation isolation, a 5-turn long alias chain, a
5-topic matrix with 3 independently-verified resolved queries (GPU,
pompa, LED), the product-without-category-word boundary, and the fused-
SSD multi-topic fix (the sprint's second primary fix).

## TEST RESULTS

`tests/test_entity_identity_semantic_alias_continuity.py` - **75
passed, 0 failed**.

## PERFORMANCE

`classify_reference_type()` ~0.013ms/call, `_strip_bounded_affixes()`
~0.005ms/call, `analyze_query()` ~0.007ms/call (2000-iteration average)
- all well under the 5ms/turn target. No network calls, no model
inference, no embeddings.

## PERSISTENT STATE SAFETY

`config/*.json` (top-level, 15 files) SHA256 + mtime confirmed byte-
identical to the exact values recorded at the end of Sprint 44 - zero
persistent-state changes. All five production edits are pure regex/
constant changes in existing Python modules; no new file I/O, no new
persistent alias/entity storage, no global topic state introduced.

## KNOWN LIMITATIONS

Carried forward, unchanged, from Sprint 44 (bare compound-noun "-nya"
declaratives like "LED strip-nya 430." still replace rather than merge -
deliberately not fixed, same reasoning as before). Newly confirmed this
sprint: a specific product name is never linked to its generic category
unless the category word was used somewhere in the conversation (e.g.
"INMP441" is never automatically understood to be a "mic" on its own) -
this is a deliberate boundary of a lexical/bag-of-terms system without
embeddings or world knowledge, not a defect, and the brief's own Part D
explicitly forbids inventing a fix for it ("do not turn every repeated
word into an entity alias"). Similarly, a multi-residual-word query
using words merely contextually associated with a topic ("performa
gamingnya" near a GPU topic) is correctly refused rather than connected,
consistent with the "do not create a giant synonym dictionary" and "do
not let entity continuity override relevance" mandates.

## INVARIANTS

No embeddings, no LLM judge, no second ranking system, no new synonym
groups, no unrestricted fuzzy matching, no general Indonesian stemmer.
`_rank_key()`, `_apply_budget()`, `assemble_context()`'s parameter list,
`ActiveTopicSnapshot`'s field set, `is_pure_reference_followup()`,
`is_merge_reference_followup()`, `is_sparse_unknown_followup()`,
`is_active_topic_relevant_to_query()`, `update_active_topic()`,
`update_topic_history()`, `_TOKEN_SYNONYM_GROUPS`/`_TOKEN_SYNONYM_
PHRASES`, `_MIN_AFFIX_ROOT_LEN` (for every pass except the new "-nya"-
only exception) all unchanged. TTS, streaming, cancellation, voice
output modes, and response/memory ranking were not touched and have no
code-path overlap with either file this sprint modified.

## FILES MODIFIED

- `luno/memory.py` - `_COMPARISON_MARKER_RE` gained one alternative;
  `classify_reference_type()`'s comparison-branch residual filter and
  `_attribute_reference_word()`'s candidate exclusion both gained one
  excluded word.
- `luno/memory_context.py` - `_TOPIC_OVERLAP_STOPWORDS` gained one
  entry; a new `_MIN_CLITIC_ROOT_LEN=3` constant used only by the
  "-nya" clitic pass inside `_strip_bounded_affixes()`.

## FILES CREATED

- `tests/test_entity_identity_semantic_alias_continuity.py` (75 tests).
- `docs/change_impact/entity_identity_semantic_alias_continuity.md`
  (this file).
- `ARCHITECTURE_GUARD.md` §45 (appended).
- `docs/testing/regression_baseline.md` Sprint 45 entry (appended).
- `docs/project_handover.md` / `docs/project_handover.json` (durable
  cross-agent handover documents, Part N/O of this sprint's brief).

## NEXT RECOMMENDED WORK

See `docs/project_handover.md` §22 ("Next Recommended Sprint") for the
full writeup. In summary: the two Sprint-44-documented known limitations
(bare compound-noun "-nya" declaratives; product-to-category world
knowledge) remain open and were re-confirmed, not re-attempted, this
sprint - both would require either a fundamentally different mechanism
(a bounded product->category lookup table, explicitly scoped and
justified per-entry) or accepting a higher false-positive risk than this
project's own invariants allow. If a future sprint wants to close either
gap, it should start from a fresh live-reproduction pass (not from this
document's assumptions) and weigh the false-positive risk explicitly
before implementing anything.
