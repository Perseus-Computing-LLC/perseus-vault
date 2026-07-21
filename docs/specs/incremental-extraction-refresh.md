# Incremental extraction and refresh for changed artifacts

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #734
Related: `provenance-classes-derived-facts.md` (classes, pipeline),
`structured-truth-retrieval-policy.md` (retrieval order),
`memory-provenance-and-external-refs.md` (origin fields, refs),
`source-anchors-corrections-retention.md` (supersession, retention)

Structured truth goes stale when source artifacts change. Re-running full
extraction over every artifact on every change is too expensive, and
re-writing everything creates duplicate observations. This spec defines
source-artifact version tracking on extracted entities, selective
re-extraction, lineage preservation, duplicate avoidance, and the
invalidation policy for capture/extract pipelines. It is a conventions
spec: it composes existing fields and tools (`derived_from`,
`mimir_supersede`, valid-time, write-time dedup) rather than adding a new
subsystem.

## 1. Source-artifact version tracking

Every `fact_extracted` entity (provenance spec §1) MUST record the version
of the artifact it was extracted from:

```json
{
  "source_artifact": {
    "ref_type": "file | confluence_page | url | custom",
    "ref_value": "canonical artifact id (anchor vocabulary)",
    "source_hash": "sha256 of extracted content, hex",
    "source_version": "optional external version (page version, etag)",
    "extracted_at": "unix ms",
    "extractor": "rule_based | llm | connector:<name>"
  }
}
```

- `source_hash` is computed over the same normalized text the extractor
  saw; it is the change-detection key. `source_version` is advisory — used
  when the source system provides one, never required.
- The artifact record itself (`source_human`, pipeline stage 1) carries the
  same hash; entities derived from the extracted facts inherit the hash
  transitively through their `derived_from` chain, not by copying it.
- Section-scoped extraction SHOULD record a `section_path` (heading path,
  line range) so a small edit invalidates only its section (see §2).

## 2. Selective refresh on artifact change

Refresh flow when a watched artifact is re-ingested:

1. **Hash compare.** New content hash vs stored `source_hash`. Equal →
   no-op; nothing is re-extracted, nothing rewritten. This is the common
   case and must be O(1) per artifact.
2. **Locate affected entities.** Entities whose `derived_from` chain
   terminates at this artifact are the refresh set. With `section_path`,
   the set narrows to sections whose bytes changed.
3. **Re-extract only the refresh set.** Run the extractor over the changed
   sections; produce candidate replacement facts.
4. **Reconcile (see §3–§4).** Unchanged facts are kept; changed facts are
   superseded, not edited; new facts are written; vanished facts are
   invalidated per §5.
5. **Propagate upward.** `fact_derived` observations whose evidence set
   intersects the refresh set are marked `stale_evidence: true` (metadata
   flag) and re-derived lazily on next consolidation pass — never eagerly
   recomputed on every artifact save.

Write-path placement: steps 1–2 belong in `mimir_ingest_file` /
`mimir_capture` / connector ingest; steps 3–5 are pipeline conventions the
caller (or a refresh helper) executes. Existing tools keep their semantics;
refresh is orchestration over them.

## 3. Lineage preservation

- Supersession, not mutation: a changed fact is replaced via
  `mimir_supersede` (new entity `supersedes` old, old entity's `valid_to`
  closed). The old version stays queryable via `mimir_as_of` / `valid_at`;
  the audit trail from current observation to source version is the
  supersession chain plus the `source_hash` recorded on each link.
- The new entity records the *new* `source_hash`; comparing hashes along
  the chain shows exactly which artifact version each version of the fact
  came from.
- `mimir_traverse` over `derived_from` + `supersedes` renders the full
  lineage: artifact v3 → fact v2 → observation (graph-first spec §3).

## 4. Duplicate avoidance

- Write-time near-duplicate merging (#531) already folds a re-extracted
  identical fact into the existing entity (`action: deduped`). Refresh MUST
  NOT pass `skip_dedup` — unchanged sections re-extracting identical facts
  are the normal case and must collapse.
- A fact that differs only by `extracted_at`/`source_hash` metadata is not
  new: reconcilers compare fact *content* (the same similarity the dedup
  path uses), updating only the source metadata in place when content is
  unchanged.
- Observations re-derived after refresh reuse the existing
  `(category, key)` so `mimir_remember` updates in place rather than
  spawning a parallel observation.

## 5. Invalidation policy

- **Fact vanished from source** (section deleted): supersede the extracted
  entity with a tombstone successor (`status: deprecated`, reason
  `source_removed`) and close its valid period at refresh time. Never
  hard-delete; history stays auditable.
- **Artifact deleted or unreachable**: mark its extracted entities
  `stale_evidence` and demote via decay; invalidate on the next successful
  fetch that confirms removal.
- **Hash unavailable** (source system gives no stable content): fall back
  to `source_version`, else to `extracted_at` freshness — and say so in the
  entity metadata so downstream trust scoring can discount it.
- Invalidation never cascades past one hop automatically: derived
  observations are flagged, and consolidation decides whether the
  observation still holds from its remaining evidence.

## 6. Success criteria mapping (#734)

- Lower compute: hash-compare short-circuits unchanged artifacts; only the
  refresh set is re-extracted (§2 steps 1–3).
- Fresher truth: section-scoped refresh lands in minutes, not at the next
  full re-ingest; stale evidence is flagged until re-derived (§2 step 5).
- Fewer duplicates: dedup-on-refresh plus content-based reconciliation (§4).
- Audit trail: supersession chain + per-version `source_hash` gives
  current-observation → source-version lineage (§3).

## 7. Implementation slice

- Add `source_artifact` metadata population to `mimir_ingest_file`,
  `mimir_capture`, and connector ingest (metadata only, no schema change).
- Refresh helper: hash-compare + refresh-set enumeration over
  `derived_from` chains; reconciliation rules of §3–§5.
- `stale_evidence` flag surfaced in served explanations until re-derived.
- Golden test: edit one section of a two-section artifact → only that
  section's facts superseded; unchanged fact returns `deduped`.
