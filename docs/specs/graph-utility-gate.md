# Graph utility gate and graph/evidence consistency

Status: implementation specification
Date: 2026-08-09
Resolves: #869
Related: `graph-first-retrieval.md` (question shapes, #735),
`fused-multi-strategy-recall.md` (#883), `retrieval-telemetry.md` (#872),
`hybrid-retrieval-ranking.md`, `data-boundaries-retention-lifecycle.md`
(#866/#868), `served-memory-api.md`

Graph traversal is only worth its cost for questions that are actually
graph-shaped. This spec is the operational contract behind #869: **when** the
graph arm runs (the utility gate), **what** an edge must carry to be
serveable (evidence/scope/lifecycle metadata), **how** the routing decision
is observed, and **how** graph/entities/indexes/receipts drift is detected.

It extends — and does not replace — `graph-first-retrieval.md`, which
describes the question shapes and the serving-layer helper composition.

## 1. Query routing: the graph utility gate

### 1.1 Classification

`src/graph_route.rs` classifies every fused-recall query into a question
shape with a utility score in [0, 1]. It is pure, deterministic, and
dependency-free (shared stopword list from `db.rs`, temporal-marker detection
from `extraction.rs`). Signals:

| Signal | Contribution (capped) |
|---|---|
| Strong connector (depends, supports, caused, derived, lineage, impact, because of, supersedes …) | 0.35 per connector, cap 0.7 |
| Weak connector (related, connects, between, through, path, chain, network, changed …) | 0.20 per connector, cap 0.4 |
| Named-entity tokens (capitalized names, acronyms, `#refs`, quoted spans, letter+digit tokens) | 0.30 per token, cap 0.6 |
| Content words (non-stopword tokens) | 0.10 per word, cap 0.2 |
| Global/overview words (overview, everything, landscape, map of …) | 0.30 |
| Temporal markers (date words, years, clock times) | 0.15 |

Reason priority: `multi_hop` > `global` > `temporal` > `entity_centric` >
`relational` > `ordinary` > `no_signal`. `multi_hop` requires two or more
connectors, or a strong connector with entity anchors / ≥3 content words, or
two named entities with a connector.

### 1.2 Threshold and engagement

- Default threshold: **0.5** (`DEFAULT_GRAPH_UTILITY_THRESHOLD`). Engaged iff
  `utility >= threshold`.
- Fused mode only. When the caller requested the `graph` strategy and the
  query clears the gate, the arm runs exactly as before (seeds = top of
  fts5 ∪ dense, one-hop expansion, RRF weight 0.5). When it does not clear
  the gate, the arm is **skipped — never a failure**: the other strategies
  serve, the strategies list carries a `"skipped"` status entry, and the
  routing reason is recorded.
- Caller override: `graph_utility_threshold` (0.0 = gate off / always
  engage; 1.0 = effectively never). Values outside [0, 1] are rejected
  fail-closed by `perseus_vault_recall` / `perseus_vault_recall_batch`.
- Temporal questions are classified `temporal` and are NOT routed to the
  graph arm: fused mode already serves them with the dedicated temporal
  strategy; the gate keeps the graph arm off a shape it does not own.
- Hybrid mode keeps its fixed graph-expansion arm (it is not a routing
  surface); the serve-time gates in §2 apply to it identically.

### 1.3 Observability (AC1, AC2)

Every fused recall response carries `fused_trace.graph_route`:

```json
{
  "utility": 0.55,
  "reason": "multi_hop",
  "selected": true,
  "skipped_reason": "",
  "unattested_edges_skipped": 0,
  "out_of_scope_edges_skipped": 0,
  "expired_targets_skipped": 0,
  "dangling_targets_skipped": 0
}
```

`skipped_reason` is empty when engaged; `"low_utility"` or `"no_signal"`
otherwise. The per-strategy trace entry for a skipped graph arm has status
`"skipped"`, candidates 0.

## 2. Serve-time edge gates (AC3, AC4)

An edge is **serveable** by the graph recall arms (`graph_expand`, used by
both hybrid and fused) only when ALL of the following hold. Every skipped
edge is counted in the arm stats and surfaced in `fused_trace.graph_route`
(and `graph_drift`):

1. **Evidence gate** — the link carries a `source` evidence anchor
   (`MemoryLink.source`). Every programmatic write path stamps the from-side
   entity id as the default anchor: `perseus_vault_link`, `remember`
   (insert and re-assert union), cohere auto-links and promotions,
   consolidate observations, dream insights, community summaries. Callers
   may supply a richer anchor (source event / external ref); it is
   preserved. Links WITHOUT an anchor (pre-#869 rows, hand-edited data) are
   **not serveable**; they surface only through `graph_drift` and
   `graph_attest`.
2. **Scope gate** — the target workspace must be the source entity's
   workspace or the global ('') partition (the recall scope convention).
   Edges into unrelated workspaces are never followed.
3. **Lifecycle gate** — the target must satisfy the same read-time
   eligibility as ordinary recall: not archived, not expired
   (`expires_at_unix_ms` in the past), status outside
   `NON_SERVEABLE_STATUSES`, and not suppressed. Superseded versions live in
   `entity_history` and are unreachable by construction (hydration reads the
   live table only), matching ordinary recall's store-level currency.

`traverse_chain` is an explicit walk, not a recall surface: it does not
hide unattested edges but annotates every edge with `attested` and `source`
(and every reached node with `edge_attested` / `edge_source`), additive and
non-breaking. `get_entity_graph` (dashboard export) likewise reports the raw
graph; the enforcement surface is the recall arm.

## 3. Drift and synchronization checks (AC5)

`perseus_vault_graph_drift` (read-only) reports, optionally workspace-scoped:

| Field | Meaning |
|---|---|
| `entities.active` / `with_links` / `embedded_active` + `embedding_coverage` | entity + vector index sync |
| `links.total` / `attested` / `unattested` | evidence-class health |
| `drift.dangling_links` | link target missing entirely (or archived) |
| `drift.links_to_archived_targets` | target archived |
| `drift.links_to_expired_targets` | target past expiry |
| `drift.out_of_scope_links` | target workspace outside {source ws, ''} |
| `drift.stale_community_memberships` | community_summary edges to missing/archived targets |
| `drift.fts_drift_estimate` | active entities vs FTS rows (index sync) |
| `drift.journal_receipts_referencing_missing_entities` | receipts whose entity is gone from entities + entity_history (informational: purge intentionally leaves receipts) |
| `consistent` | true iff every structural check above is clear |

`perseus_vault_graph_attest` (workspace-scoped, `dry_run` preview) stamps the
from-side entity id on legacy edges, one audited transaction per entity, and
journals one `graph_attest` event. After attestation `graph_drift` reports
`unattested = 0` for the covered scope. This is the migration path for
pre-#869 data; new writes are attested by construction.

## 4. Benchmarks (AC6)

`benchmark/quality` manifest v5 adds the `graph_gate` scenario
(metric `graph_utility_gate`):

- `graph-gate-multi-hop-routes` — a multi-hop/impact query engages the graph
  arm with reason `multi_hop`, and the linked neighbor surfaces through it.
- `graph-gate-ordinary-falls-back` — an ordinary single-hop query falls back
  without failure: graph status `skipped`, reason `ordinary`, keyword arm
  still serves.
- `graph-gate-temporal-not-routed` — a temporal question is classified
  `temporal` and not routed to the graph arm.
- `graph-gate-consistency` — an all-attested in-scope fixture keeps
  `graph_drift` consistent and the graph arm serves zero fabricated
  (unattested) edges; evidence reports `graph_arm_latency_ms`,
  `graph_arm_candidates`, and the fabricated-edge rate.

## 5. Compatibility

- `MemoryLink.source` is additive: stored links JSON without the field still
  parses (`#[serde(default)]`); old databases open unchanged.
- Recall responses gain `fused_trace.graph_route` only when the `graph`
  strategy was requested (additive; existing consumers unaffected).
- `graph_expand` returns `(candidates, GraphArmStats)` — an internal API
  change; the MCP surface is unchanged except for the two new tools and the
  new recall parameter.
- Write-path stamping means edges created by current software are attested
  by default; only pre-#869 rows require `graph_attest`.
