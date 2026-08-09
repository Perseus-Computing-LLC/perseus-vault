# Fused Multi-Strategy Recall (#883 + #867)

Status: implemented (branch `feat/vault-883-fused-recall`)
Issues: #883 (TEMPR-style multi-strategy recall), #867 (recall telemetry /
traceability contract)
Mode name: `fused` — recall tool mode alongside `fts5` / `dense` / `hybrid`.

## Motivation

Hybrid recall fuses exactly two arms (BM25 + dense). A fact that the query
does not lexically or vectorially match — but that is *related* (graph
neighbor), *temporally relevant* (created near the query's instant), or
*corroborated* (matches several independent strategies) — is invisible to it.
`fused` generalizes the fusion to **four strategies** with explicit
traceability, bounded token budgets, and an optional rank-calibrated rerank
stage.

## Strategies

| strategy  | source arm                                    | contribution                            |
|-----------|-----------------------------------------------|-----------------------------------------|
| `fts5`    | `fts5_bm25_search` (stopword-filtered)        | keyword relevance                       |
| `dense`   | embedding backend (local ONNX or provider)    | semantic similarity                     |
| `graph`   | one-hop neighbor expansion off top seed set   | relational discovery (never invents)    |
| `temporal`| proximity of keyword matches to `query_time`  | time-anchored ranking                   |

- `fts5` and `dense` run in parallel (wave 1); `graph` and `temporal` run
  after (wave 2, both depend on the keyword match set).
- **Fail-closed:** the keyword arm is core — its failure fails the recall.
  The dense arm *degrades* (empty arm + `degraded` note) instead of failing,
  and a degraded arm marks the outcome `Partial` with the arm named in
  `partial_arms`.
- An arm that is not engaged (or yields nothing) contributes **weight 0**:
  fusion degenerates gracefully to the remaining arms.
- **Bounded graph:** expansion is one hop (`graph_expand`, v1 semantics).
  A two-hop entity is *not* surfaced — documented bound, enforced by the
  seed-exclusion set (any entity that itself matched is a seed and is not
  re-introduced by the graph arm).
- **Currency:** the store is `UNIQUE (category, key, workspace)` — one live
  version per key — so the temporal arm ranks the *current* version by
  proximity. Point-in-time (bi-temporal) bodies are reconstructed
  downstream by the existing `as_of` / `valid_at` handler path, which
  composes with fused recall unchanged.

## Fusion

Weighted Reciprocal Rank Fusion (RRF) over the arm rankings:

```
score(e) = Σ_arms  w_arm · 1/(k + rank_arm(e))        k = 60 (v1)
```

- `strategy_weights` overrides per-arm weights (default `1.0`).
- Weights are validated **fail-closed**: unknown strategy name, unknown
  weight key, negative weight, or non-finite weight → `Err`, no partial
  execution.
- At least **2 strategies** are required (validation, `Err` otherwise).
- Post-fusion pipeline mirrors `hybrid`: usefulness boost, scope
  resolution, supersede, layer filter, metadata filters, suppressed
  (tombstoned) exclusion, then truncation.

## Token budget & depth budget

- `max_tokens` truncates the fused ranking to a token estimate
  (`chars/4`, min 1 per entity). The **top entity is always delivered**
  (min-1 semantics, documented).
- `depth_budget` maps to default caps when `max_tokens` is 0:
  `low = 1024`, `mid = 4096`, `high = 16384`.
- Accounting is reported in the trace (`budget_tokens`,
  `estimated_tokens_used`, `retained`, `dropped`) so callers can detect
  truncation.

## Optional rerank stage (default off)

`rerank=true` re-scores the fused pool with **rank-derived** signals
(`1/(1+rank)` per arm, combined `0.6·dense + 0.4·bm25`). Rank-derived
signals are scale-free by construction (no raw score of a different scale
is ever summed) and always distinct, so calibration never degenerates on
tied raw scores. When no score-bearing arm is engaged, the RRF order is
kept and the trace notes the fallback. A provider cross-encoder is the
documented extension point (`method: "rankcal-dense-bm25"` in v1).

## Traceability (#867)

Every fused recall returns a `fused_trace` (attached to the recall response
when the mode is `fused`):

- `original_query` — verbatim, **never rewritten/expanded** (exact
  identifiers like `ERR7781` must survive; no paraphrase expansions in
  fused mode).
- `expansions` — always empty in v1 (documented).
- `strategies[]` — per-arm: name, status (`ok` | `empty` | `degraded` |
  `skipped`), candidate count, elapsed ms, per-arm ordering (pre-fusion
  rankings).
- `fusion` — RRF k, weights actually applied.
- `truncation` — budget/used/retained/dropped.
- `rerank` — enabled/applied/method/note.
- `placement` — final delivered id order; `sources[id]` — which arms
  contributed each delivered entity.

## Outcome mapping

- Any degraded arm → `Partial` with `partial_arms` naming it.
- Otherwise the outcome follows the hybrid contract (fresh/empty/stale
  per completeness).

## Validation summary (tests)

`fused_recall_*` suite in `src/db.rs`:

- consensus outranks single-strategy; trace completeness (original query,
  weights, placement, sources)
- temporal proximity (nearest to `query_time` first) and store-level
  currency (superseded text un-serveable)
- graph multi-hop bound (one hop surfaces, two hops stay out)
- token budget truncation (budget 1 → top entity only; unbounded → all)
  and depth-budget defaults
- input validation: <2 strategies, unknown strategy, unknown weight key,
  negative weight, non-finite weight — all rejected
- rerank: applies when a score-bearing arm is present; falls back with a
  note when the pool is empty; disabled by default
