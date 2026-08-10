# Declared Exact-Match Retrieval Arm (#923)

Status: implemented (schema unchanged, registry 120→122 — 2 new tools:
`perseus_vault_declared_schema_set`, `perseus_vault_declared_query`; fused
recall gains the `declared` strategy).

## Problem

Recall is FTS5 keyword + semantic + temporal — all ranked/fuzzy. A
deterministic retrieval contract (agents declare typed fields; retrieval is
exact-equality filters, facet counts, and query guidance — no ranking, no
hallucination) is deliberately absent. This issue adds that arm as an
optional, read-side annotation, without adopting agent-owned ungoverned
schema lifecycles (conflicts with admission control, keystones, authority).

## Design

**Declarations** live as governed entities in the reserved category
`declared_schema` (key = the declared category name), written only through
`perseus_vault_declared_schema_set` with fail-closed validation:

- 1–32 fields, unique non-empty names; reserved vault-managed names refused
  (`id`, `category`, `key`, `recall_when`, `origin`, `external_refs`,
  `expires_at`).
- Two types: `scalar` (exact string equality) and `string_list` (array
  membership). Up to 16 facet-eligible fields. `query_guidance` ≤ 500 bytes.
- Re-declaring bumps a monotonic `version` (idempotent upsert through the
  skip-dedup remember path — same reserved-entity pattern as the guide).

**Field values** are read from each entity's own top-level `body_json` keys
at query time. Entities whose values do not conform to their declaration are
simply not matchable through that field (read-side lenient); malformed
*filters* are rejected fail-closed.

**`perseus_vault_declared_query`** — the pure arm:

- Filters are AND-combined exact-equality checks: scalar fields match by
  exact string equality; `string_list` fields match if any stored string
  equals any filter string (membership).
- Results are returned in **deterministic order (created_at ASC, id ASC)
  with no ranking**; `total_matches` + `truncated` support paging.
- Facet counts are **truthful and bounded**: requested fields must be
  facet-eligible; counts are computed over the rows passing every filter
  EXCEPT the facet's own (standard refine-by-facet semantics), capped at 50
  distinct values with an `"other"` roll-up bucket.
- The response carries the schema summary (fields, `query_guidance`,
  version) — the discovery surface (shape-only bootstrap + facets).
- Fail-closed: undeclared category, unknown filter field, type-mismatched
  filter, or non-facet facet request is an **error** — never degraded to
  fuzzy recall. Suppression (erasure mandates) is respected.

**Fused integration** (`perseus_vault_recall` mode `fused`, strategy
`declared`):

- `declared_category` + `declared_filters` (both required together) engage
  the exact arm; filters without the strategy, or the strategy without
  filters, are caller errors. `declared_category` must match `category`
  when both are set.
- The declared arm runs **before** the ranked waves; its matches are pinned
  ahead of the fused pool (RRF order among themselves), and the semantic
  arms fill the remaining limit/token budget as fallback/fusion input.
- Trace: `fused_trace.strategies` gains a `declared` entry (candidates,
  top ids, status); pinned ids get `declared` in `fused_trace.sources`;
  `fusion.weights.declared` records engagement.
- Embedding-independent: the declared arm touches no embeddings and works
  in the lite (`--no-default-features`) build.

## Scope boundaries

- Read-side only: declarations never gate writes (write governance stays
  with admission/epistemic states #880).
- No schema lifecycle, no migration machinery: a declaration is a body on a
  governed entity — versioned by upsert, forgettable, time-travelable.
- Optional: categories without a declaration behave exactly as today; the
  `declared` fused strategy is off by default (not in the implicit all-arm
  set unless filters are passed... the strategy IS in the default set but
  requires declared inputs, so it no-ops for ordinary fused recalls only
  when absent — see Contract).

## Contract

- `perseus_vault_declared_schema_set(category, fields[], query_guidance?)`
  → `{ok, category, version, fields, query_guidance}`.
- `perseus_vault_declared_query(category, filters?, facets?, limit?,
  offset?, workspace_hash?)` → `{ok, category, schema, total_matches,
  truncated, items, facet_counts}`.
- Fused recall with `declared_category` + `declared_filters` engages the
  `declared` strategy (it is in the implicit all-arm set; an EXPLICIT
  strategy list that names `declared` without the inputs is an error).
  Ordinary fused recalls without declared inputs are unchanged — the arm
  simply skips.

## Verification

- Unit/integration: schema-set validation fail-closed (unknown type,
  duplicate/empty/reserved names, field/facet caps, guidance cap); exact
  scalar equality + string_list membership + AND semantics; deterministic
  no-ranking order; paging; facet truthfulness + `"other"` bound;
  suppression respected; undeclared-category / unknown-field /
  type-mismatch / non-facet-facet rejections; fused pinning (declared
  matches first, semantic fallback under budget); trace entries; lite build.
