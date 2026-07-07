# Feature Spec: Temporal RAG — point-in-time semantic recall

**Status:** proposed
**Depends on:** existing bi-temporal engine (`as_of`, `valid_at`, `bitemporal_at`, entity history), hybrid recall (FTS5 + dense + RRF)
**Competitive driver:** Zep/Graphiti win LongMemEval on valid-time *fact lookup*; nobody offers valid-time *semantic search*.

## Problem

Perseus Vault already has the strongest temporal *point-lookup* surface in the
category: full SQL:2011 bi-temporal queries (`as_of` = transaction time,
`valid_at` = application/world time, `bitemporal` = both). But those answer
questions about **one fact you can already name** (`category` + `key`).

**Semantic recall (`recall`, `ask`, `global_recall`) is live-only.** There is no
way to ask:

- "Search my memory *as I believed it on 2026-03-01*" (transaction-time RAG) —
  reproduce what an agent would have retrieved before a later correction landed.
- "Find everything that was *true in the world during Q1 2026*" (valid-time RAG) —
  retrieve the state of knowledge for a past world-window, not today's.

Competitors stop at valid-time *fact retrieval by entity*. Valid-time *ranked
semantic search over the whole store* is unclaimed territory and is the natural
superset of what Zep markets. It is also the audit/compliance story: "show me
exactly what the agent could have known at decision time."

## Proposal

Add a temporal filter to the recall path. Two orthogonal, optional parameters,
mirroring the point-lookup tools so the mental model is identical:

| Param | Axis | Meaning |
|---|---|---|
| `as_of_unix_ms` | transaction | rank only over versions recorded at/before T; use the body live at T |
| `valid_at_unix_ms` | application | rank only over facts whose valid period contains T |

Both may be combined (full bi-temporal recall). Absent → today's live view
(current behavior, zero change).

### Semantics

1. **Candidate generation** runs unchanged (FTS5 + dense over the live index)
   but is widened to include superseded versions when a temporal filter is set.
2. **Temporal gate** (reuses `versions_recorded_by` + the `bitemporal_at`
   interval logic — the code just fixed for out-of-order arrival) filters each
   candidate entity to the single version that satisfies the requested
   (tx, valid) instant, or drops it if none does.
3. **Ranking** (RRF, trust, decay, recency) runs over the gated set, scoring the
   *point-in-time body text*, not the live body.
4. Result rows carry `recorded_at_unix_ms` / `valid_from`/`valid_to` and an
   `is_live_version` flag so callers can see they got a historical view.

### Surface

- Extend `mimir_recall` with `as_of_unix_ms` + `valid_at_unix_ms` (already have
  `valid_at` filtering on live rows via `valid_periods_for_ids`; this generalizes
  it to historical versions).
- Extend `mimir_ask` / `mimir_global_recall` to thread the same two params into
  their internal recall call so RAG answers are reconstructable at a past instant.
- All three keep working with zero params = zero behavior change.

## Why this wins

- **Superset of Zep's headline.** They do valid-time; we do valid-time *and*
  transaction-time *and* the cross-product — over ranked semantic search, not
  just entity lookup.
- **Compliance-grade.** "Reproduce the exact retrieval context the agent had at
  decision time" is a hard requirement in regulated (defense/finance/health)
  deployments and is impossible on a live-only store. This is the enterprise
  moat.
- **Local-first, zero new deps.** Reuses the bi-temporal engine and the existing
  hybrid ranker. No graph DB, no cloud, no new storage — unlike Cognee's
  poly-store or Zep's Graphiti requirement.

## Acceptance criteria

- `recall(query, as_of_unix_ms=T)` returns bodies as they were believed at T;
  a fact corrected after T does not leak its corrected body into the T-view.
- `recall(query, valid_at_unix_ms=T)` returns only facts whose valid period
  contains T, ranked by relevance, including facts whose live version is a later
  world-period.
- Combined params satisfy the bi-temporal rectangle (consistent with
  `bitemporal_at`).
- No temporal params → byte-identical to current recall output.
- New harness `benchmark/temporal/temporal_rag.py` drives the real binary and
  gates in CI, extending the gauntlet from point-lookup to ranked retrieval.

## Non-goals

- Not a time-series database; instants are queried, not aggregated over ranges
  (a `valid_during(start,end)` range variant is a possible follow-up).
- No change to write path or storage schema — history already persists versions.
