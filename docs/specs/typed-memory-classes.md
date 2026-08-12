# Typed memory classes (#1000)

Status: normative. Surface: `memory_type` on remember/recall/entities,
`perseus_vault_type_policies` (MCP tool, read-only).

Borrowed from CogniCore's `MemoryType` taxonomy (8 classes + per-type
`TypePolicy`), mapped onto the vault's governance regime. The borrow is the
TAXONOMY and the idea that a class carries behavior; the enforcement is the
vault's own.

## The 8 classes

| memory_type | decay_multiplier | retrieval_weight | rationale |
|---|---|---|---|
| semantic (default for legacy rows) | 1.0 | 1.0 | baseline; '' resolves here |
| episodic | 0.5 | 0.9 | events rot faster, rank lower |
| procedural | 2.0 | 1.2 | proven recipes outlive and outrank |
| preference | 4.0 | 1.3 | near-durable user signal |
| constraint | 4.0 | 1.4 | most durable, most rank-worthy |
| failure | 1.5 | 1.3 | anti-patterns keep resurfacing |
| reflection | 1.2 | 1.1 | insights earn rank by usefulness |
| knowledge | 1.5 | 1.0 | durable, neutral rank |

## Semantics

- **Write**: `perseus_vault_remember` accepts `memory_type`. Unknown values
  are a HARD write error (fail-closed — deliberate divergence from
  CogniCore's silent SEMANTIC fallback, per the #998 fail-loud convention).
  Omitted/empty = legacy row: stored as '' (never rewritten), governed by
  the SEMANTIC policy.
- **Decay**: `decay_multiplier` scales the #941 per-category half-life at
  decay tick (composes — a category override still wins; "never" survives
  any finite multiplier). Verified floor and the #487/#681 efficacy
  composites still apply, and the explicit-importance floor still applies
  LAST.
- **Retrieval**: `retrieval_weight` multiplies the final fused score in the
  honest-usage rank loop (same site as the usefulness × efficacy terms).
  Legacy rows keep 1.0, so pre-#1000 behavior is byte-identical.
- **Filter**: `type_filter` on recall applies to EVERY mode (FTS, Dense,
  Hybrid, Fused) — validated fail-closed at the public `recall` entry,
  filtered after the mode-specific path (fused also filters pre-truncation
  for limit quality; both are idempotent). Legacy rows ('' ) satisfy a
  `semantic` filter.
- **Storage**: schema v41 adds `entities.memory_type TEXT DEFAULT ''`
  (additive; pre-v41 rows read as '' = legacy).

## Governance relevance

The four classes the vault's governance regime cares most about —
PROCEDURAL, CONSTRAINT, FAILURE, REFLECTION — are precisely the ones with
above-baseline durability and rank. Constraints and preferences outlive
episodes by design; failures outrank raw similarity so anti-patterns keep
resurfacing until the lesson is learned.
