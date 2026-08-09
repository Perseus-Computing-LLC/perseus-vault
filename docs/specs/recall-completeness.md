# Recall Completeness & Bounded-Search Semantics (#856)

Status: implemented (branch `feat/vault-856-dense-completeness`)

## Problem

Dense and hybrid recall rank candidates from a **bounded pool**
(`candidate_k = limit*5`, capped at 1000, in the historical implementation).
Metadata filters (category, type, topic, workspace, layer) are applied
*after* ranking, so an in-scope hit that ranks below the pool boundary is
silently lost — the caller sees a short result and cannot tell whether the
store genuinely has nothing more, or the search simply did not look far
enough. FTS5 does not have this problem (the index ranks the full match
set), which made the semantic paths the silent offender.

## Contract

Every recall now carries top-k completeness metadata. The outcome block on
`perseus_vault_recall` (with `include_outcome`, and always on degraded responses)
and the standalone `perseus_vault_recall_outcome` expose:

```
completeness: exact | bounded | partial | abstain
candidate_scope: { scanned: int, embedded_population: int|null, pool_bound: int|null }
```

| value | meaning |
|---|---|
| `exact` | the ranking was computed over the **complete** embedded scoped population (the scan was exhausted — `returned < pool` — so every embedded row was examined). FTS is always `exact`. |
| `bounded` | the scan hit its pool bound, but the requested `limit` in-scope hits were still found. The returned top-k is not provably the true top-k (a larger pool could reorder it), but it is not truncated short. |
| `partial` | the scan hit its pool bound **and** returned fewer than `limit` in-scope hits — an incomplete top-k. Callers should treat the result as provisional. |
| `abstain` | no evidence / backend unavailable (the #864/#887 abstention contract wins over everything). |

`candidate_scope.scanned` = embedded rows actually examined;
`embedded_population` = the full embedded population (only known when the
scan was exhausted); `pool_bound` = the candidate pool ceiling used (only
set when the scan was *not* exhausted).

## Adaptive over-fetch

The historical fixed pool is replaced by an adaptive driver shared by the
dense and hybrid arms:

1. Start at the historical pool: `max(limit*5, limit)` clamped to 1000.
2. Run the arm over the pool; apply suppression, scope-rank weighting, and
   metadata filters.
3. If `limit` in-scope hits were found **or** the scan was exhausted
   (`returned < pool`) **or** the pool ceiling (4096) was reached, stop.
4. Otherwise double the pool and re-run (hard safety cap: 12 attempts).

So a category-scoped recall over a corpus where the in-scope rows rank
deep keeps expanding until the scoped results surface or the whole embedded
population has been examined. Worst-case work is bounded by the ceiling
(4096 candidates × up to ~10 passes), which is a deliberate, documented
latency/completeness trade-off; latency beyond that is reported via the
existing `deadline_ms` / `outcome.status = timeout` contract (#864).

## Semantics notes

- **Conservativeness:** when the scan is not exhausted, `bounded` is
  reported even in the sig-cache regime (where the cache actually ranks the
  full corpus). This under-claims exactness rather than over-claiming it —
  we cannot cheaply distinguish the regimes at the recall surface.
- **Abstention wins:** if the outcome is `abstained` (no evidence, dead
  backend), completeness is `abstain` regardless of pool dynamics.
- **FTS:** no pool exists — `exact`, `candidate_scope: null`.
- **Standalone `perseus_vault_recall_outcome`:** a conservative *estimate* derived
  from the scan bound vs the embedded population (`exact` when
  `population <= PERSEUS_VAULT_DENSE_MAX_SCAN`; `bounded` otherwise; `abstain` on
  abstention). The recall path's own pool dynamics (above) are authoritative
  and override the estimate on `perseus_vault_recall` responses.

## Behavior preserved

- `limit` is still honored exactly (truncation after the adaptive loop).
- Byte-stable deterministic ordering on frozen DBs (#254) is untouched: the
  loop re-runs the same deterministic arm functions with a larger pool.
- The pool ceiling and the `PERSEUS_VAULT_DENSE_MAX_SCAN` dial (#619) are
  independent: the ceiling bounds recall-side candidate generation; the env
  dial bounds the underlying SQL scan.

## Tests

`dense_recall_adaptive_overfetch_finds_deep_scoped_hits`,
`dense_recall_bounded_completeness_when_pool_fills`,
`hybrid_recall_reports_completeness_and_finds_deep_hits`,
`fts_recall_completeness_is_exact`,
`recall_outcome_completeness_estimate_matches_scan_bound` — direction-based
similarity fixtures (cosine is scale-invariant) that pin the deep-drowning
and top-ranked regimes for both semantic modes plus the FTS and standalone
outcome paths.
