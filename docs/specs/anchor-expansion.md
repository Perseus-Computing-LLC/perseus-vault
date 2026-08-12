# Active-decision anchor query expansion (#1009)

Status: normative. Surface: `anchor_expansion` on recall args (opt-in),
`anchor_matched` in the fused trace.

Borrowed from MindCache's anchor retrieval (top-k ACTIVE decisions used as
extra BM25 queries so standing policy surfaces on queries with zero semantic
overlap). The vault's anchor set is strictly richer: keystones (#683,
mandatory policy rules) plus ACTIVE decision entities, where ACTIVE is a
STRUCTURAL fact — no successor claims the entity via the #363/#472 supersede
chain — not an LLM status label.

## Mechanics

1. **Anchors** (`anchor_expansion::load_anchors`): keystones (weight-ranked)
   + live, un-superseded entities in the configured anchor categories
   (`PERSEUS_VAULT_ANCHOR_CATEGORIES`, default `decision`), recency-ranked,
   k per source (default 3). Workspace-scoped: a row with another workspace
   never crosses the boundary; global ('' ) rows apply everywhere.
2. **Queries**: anchor text → sanitized lexical queries (up to 8 longest
   tokens per anchor, OR-ed, quoted) — BM25-style FTS5 expansion, not
   fusion.
3. **Boost**: fused candidates are re-checked against the anchor queries via
   `entities_fts`; each match multiplies the final score by 1.15, cumulative
   cap 1.5. Read-only — ranking only, no access-state writes (#247).
4. **Trace**: matched entity ids are recorded in `FusedTrace.anchor_matched`
   — anchor influence is surfaced, never silent.

## Guards

- **Anchor domination cap**: per-match and cumulative caps — anchors steer
  (a standing policy gets a hearing), the raw-query arm keeps the floor.
- **Opt-in**: `anchor_expansion` defaults to false; a default recall stays
  byte-identical (#247 determinism).
- **Workspace scoping**: anchors are per-workspace (+ global), never
  cross-tenant.

## Evaluation

Guarded for the #916 MemConflict set: anchor boost strength and k are the
sweep knobs under the #954 equal-token-budget methodology.
