# Tool Lifecycle Policy (#1052)

**Status:** adopted (2026-08-14) · **Scope:** the canonical MCP tool registry
(`src/mcp.rs` embedded registry, `TOOL_SCOPES` side table,
`scripts/registry_metadata_check.py`)

## Audit result (2026-08-14)

Cross-referenced all **147 canonical MCP tools** (main `b85d118`) against
`docs/`, `README.md`, `CLAIMS-AUDIT.md`, `integrations/`, benchmark harnesses,
and test modules. **No dead tools found**: every tool retains a registry entry,
a dispatch arm, a handler, and in-module tests. The 147-tool count is
accumulated feature-milestone surface, not dead code.

## Soft candidates (tiered, NOT deleted)

| Tool | Why it stays |
| --- | --- |
| `perseus_vault_migrate` | One-shot v0.1.x→v0.5.0 migration; legacy but still the supported upgrade path. Tiered `admin`. |
| `perseus_vault_reindex` | Overlaps one op inside `perseus_vault_maintenance`; targeted recovery tool, cheap to keep. |
| `perseus_vault_bitemporal` | Convenience composition of `as_of` + `valid_at`; distinct SQL:2011 query contract. |

## Deletion-review policy

A tool becomes a **trim candidate** only when it is BOTH:

1. **zero-referenced** in `docs/`, `integrations/`, benchmark harnesses, and
   README tool-family sections for **>= 3 consecutive releases**, AND
2. **superseded** by a newer tool whose contract covers it (documented in the
   newer tool's description or a spec doc).

A trim candidate is still **not deleted** without a deprecation cycle: it must
ship as a deprecation alias for at least one major release (see
[tool consolidation & deprecation aliases](tool-consolidation-deprecation.md)),
and its removal is a review item on the release checklist, not an incidental
refactor.

**Current candidate list: empty.**

## Enforcement

- `scripts/registry_metadata_check.py` keeps the registry literal, the
  `TOOL_SCOPES` side table, and the metadata surfaces (README, CLAIMS-AUDIT,
  manifest, server, glama) in lockstep on every CI run — the count can only
  move by explicit, reviewed change.
- Tool-scope advertisement tiers (#1051, `PERSEUS_VAULT_TOOL_SCOPE`) cut the
  default agent surface from 147 to 48 without any contract churn; consolidation
  design is tracked in #1053.

## Review cadence

Re-run the cross-reference audit at every release boundary (or when a PR
touches the registry) and update this document's audit line. Candidates that
meet both criteria move to the candidate list; anything else stays.
