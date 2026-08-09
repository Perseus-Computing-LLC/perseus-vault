# Evidence-grounded observations

Status: implementation specification
Date: 2026-08-09
Resolves: #884
Related: `memory-taxonomy-and-precedence.md` §5 (beliefs overlay, #717),
`epistemic-trust-axes.md` (#880/#881), `validity-aware-recall.md` (#860),
`multi-agent-scoping.md` (#854)

## Motivation

Competitive scan 2026-08-07 (Hindsight): consolidation produces deduplicated
beliefs grounded in specific source memories — each observation carries
exact-quote evidence refs, a proof count, is refined (not overwritten) when
new evidence supports/contradicts/extends it, preserves the full journey
("was React, switched to Vue"), and is freshness-aware (newer unconsolidated
facts mark it stale, so ask verifies it against raw facts before use).

## Observation entity schema (body v2)

Category `observation`. v1 bodies (summary/source_ids/proof_count/
merged_from_category) parse tolerantly and migrate to v2 on the next
consolidate staleness refresh.

```json
{
  "summary": "stack switched to vue",
  "source_ids": ["mem-1", "mem-2", "mem-3"],
  "quotes": [
    {"source_id": "mem-1", "quote": "stack uses react"},
    {"source_id": "mem-3", "quote": "stack switched to vue"}
  ],
  "proof_count": 3,
  "merged_from_category": "tech",
  "updated_at_unix_ms": 1750000000000,
  "stale": false,
  "history": [
    {"from": "stack uses react", "to": "stack switched to vue",
     "changed_at_unix_ms": 1750000000000, "triggered_by": "mem-3",
     "reason": "contradiction"}
  ]
}
```

- `quotes`: exact-quote evidence refs — each source's `note` verbatim
  (else the body), capped at `quote_cap_chars` (64..=4096, default 512,
  validated fail-closed) with an ellipsis marker.
- `history`: the preserved journey. Entries are append-only; a fold never
  creates one, a contradiction does.
- `stale`: derived flag — a newer unconsolidated fact exists in
  `merged_from_category` (created after `updated_at_unix_ms`, not already in
  `source_ids`). Read-time computation wins over the stored snapshot.

## Consolidation behavior (perseus_vault_consolidate)

New params: `refine_existing` (default true), `quote_cap_chars` (default 512).

1. **Clusters (existing union-find, threshold-configurable)**: members
   already folded into an observation are skipped (idempotent re-runs).
   The cluster's best body is matched against existing live observations
   about the same category by trigram similarity:
   - sim ≥ threshold → **fold** (quotes + source_ids + proof_count,
     updated_at bumped, no journey entry);
   - 0 < sim < threshold → **refine**: journey entry appended
     (`from` = old summary, `to` = the contradicting source's text,
     `triggered_by` = its id, `reason` = "contradiction"), summary
     advances; later sources classify against the revised summary;
   - no overlap → **fresh observation** (v2 body; stale=false by
     construction).
2. **Singleton pass**: unclustered, unfolded new facts get the same
   fold/refine treatment against their best match — this is the correction
   path (a lone contradicting fact reconciles into the journey). With
   `refine_existing=false`, legacy behavior returns (singletons untouched,
   fresh clusters create new observations).
3. **Staleness refresh**: after writes, every live observation about the
   category gets its `stale` flag recomputed and the stored body updated
   when it differs (v1 bodies migrate to v2 here). The flag update is a
   lightweight body_json write — derived state; the body itself only
   changes through the audited re-assert path.
4. **Archive policy**: folding/refining NEVER retires sources (they are the
   evidence trail for trace-back). `archive_sources` applies only to the
   members of freshly created observations.

Audit: fold/refine writes re-assert the observation entity through
`remember`'s audited path — the prior row is snapshotted into
`entity_history` (#371), so refinement is versioned, never a silent
overwrite. Evidence links (`evidence_for`, `MemoryLink.source` = writing
observation, #869) are maintained on creation; sources keep their ids.

Report additions: `observations_refined`, `observations_refreshed`,
`observations_stale`, `quotes_captured`; each `Observation` gains
`updated_at_unix_ms`, `stale`, `refined`, `quotes`.

## Ask staleness gate

`perseus_vault_ask` (new param `verify_stale_observations`, default true —
fail-closed) runs every observation candidate through the gate before
context assembly:

- not stale (or not an observation) → cited normally;
- stale + newest unconsolidated fact consistent (sim ≥ threshold, or no
  shared trigrams = unrelated) → cited with a "verified against raw facts"
  note (`sources[].verification = "verified_against_raw"`);
- stale + newest fact contradicts (0 < sim < threshold) → **refused**:
  excluded from context, reported in `refused_sources` with
  `reason = "stale_observation_unverified"` and `detail` = the newest
  contradicting fact's key (trace-back). If every candidate is refused,
  ask errors with the refusal count.

## Acceptance criteria → evidence

| criterion | mechanism |
|---|---|
| schema includes evidence refs (memory id + quote), proof count, updated_at, staleness flag | v2 body; `consolidate_*` integration tests; `evidence-observations-*` benchmark cases |
| consolidation dedups near-duplicates (configurable threshold) and folds evidence on merge | cluster fold + singleton fold; `consolidate_folds_singleton_evidence_*`, `consolidate_cluster_folds_*` |
| correction flow preserves the journey (React → Vue), raw facts intact | `consolidate_preserves_correction_journey` (entity_history version + live sources); `evidence-observations-journey` |
| ask refuses to cite a stale observation without verification | `ask_gate_refuses_contradicted_stale_observation`; verification note on consistent/unrelated staleness |

## Compatibility

Additive surface changes only: two new optional consolidate params, one new
optional ask param, four new report fields, extended observation body. v1
observation bodies remain valid (tolerant parse + migration on refresh).
Registry tool count unchanged (106).
