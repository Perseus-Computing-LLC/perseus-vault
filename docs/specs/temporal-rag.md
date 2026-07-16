# Feature Spec: Temporal RAG — point-in-time semantic recall

**Status:** implemented (#472 wired the temporal params + point-in-time reconstruction; #682 closed the history-inclusive candidate gap)
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

1. **Candidate generation.** Live FTS5 + dense run unchanged. When a temporal
   filter is set they are *widened* to include superseded versions
   (`augment_temporal_with_history`, #682): superseded/retired bodies leave the
   live FTS index, so they are indexed in a dedicated standalone
   `entity_history_fts` (schema v20). The augmentation queries that index to
   discover the `(category,key)` pairs the live arm missed — a fact whose
   query-matching version was later replaced (e.g. "CEO is Alice" → "Bob"), or a
   key fully retired since the instant.
2. **Temporal gate** (reuses `versions_recorded_by` + the `bitemporal_at`
   interval logic — the code just fixed for out-of-order arrival) filters each
   candidate entity to the single version that satisfies the requested
   (tx, valid) instant, or drops it if none does. History-discovered keys are
   reconstructed through the **same** `bitemporal_at`/`as_of_version` engines, so
   there is one source of temporal truth.
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

## Implementation note (#682)

The original spec assumed candidate widening needed no schema change ("history
already persists versions"). In practice a *superseded* body leaves the live
FTS index the moment it is replaced, so it is unreachable by keyword/dense
search — the point-in-time reconstruction in `temporal_resolve` could only
rebuild facts whose *current* body still matched the query. Closing the gap
therefore required a searchable history surface:

- **Schema v20:** standalone `entity_history_fts` (FTS5 over history body text).
- **Plaintext, decrypt-aware:** mirrors `entities_fts` — under encryption the
  stored `entity_history.body_json` is ciphertext, so it is decrypted
  (`build_aad` with legacy fallback) before indexing; auth failure indexes an
  empty body rather than leaking/garbling ciphertext.
- **Maintenance:** indexed at the single history-append site
  (`snapshot_live_row_to_history`), cleared at both history-delete sites
  (purge/forget) to avoid orphaned text and rowid reuse, and rebuilt/backfilled
  by `reindex_fts` (the `mimir_reindex` tool) — the one-time upgrade path.
- **Additive & safe:** the augmentation only appends to fill the caller's limit
  and never reorders live hits; the hot `db::recall` core is untouched, so
  determinism (#247) and benchmark numbers are unaffected.

Follow-up (not yet done): a dense/vector arm over history (today's discovery is
keyword/FTS only), and a `benchmark/temporal/temporal_rag.py` ranked-retrieval
harness extending the point-lookup gauntlet.
