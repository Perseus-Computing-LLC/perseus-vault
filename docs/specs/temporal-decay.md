# Type-Conditioned Temporal Decay — Perishability + Utility Horizon (#1091)

Borrow: ScrubJay-MEM (arXiv:2608.04746). Implemented as the completion of
#1000's typed-class system, which gave each memory type a half-life
multiplier and retrieval weight but deliberately no surfacing-age ceiling.

## Design

Two per-type values, resolved deterministically from the stored
`memory_type` (`src/temporal_decay.rs::profile_for`):

| type        | perishability_days | utility_horizon_days |
|-------------|-------------------:|---------------------:|
| semantic    | 90                 | 365                  |
| episodic    | 14                 | 30                   |
| procedural  | 180                | never                |
| preference  | 180                | never                |
| constraint  | 180                | never                |
| failure     | 7                  | 21                   |
| reflection  | 14                 | 60                   |
| knowledge   | 90                 | 365                  |
| legacy (`""`) | 90 (semantic)    | 365                  |

- **Perishability** documents the type's intrinsic useful lifetime. The
  half-life multiplier from #1000 remains the decay engine — perishability
  is the audit baseline, so the decay tick is not double-conditioned.
- **Utility horizon** is a hard surfacing-age ceiling applied at recall
  time (`Database::recall` post-filter): an entity older than its type's
  horizon is excluded from default recall regardless of residual decay
  score. Durable types carry the `HORIZON_NEVER_DAYS` encoding shared with
  the decay tick's never-half-life convention.
- The gate is query-adaptive: `RecallParams.enforce_utility_horizon`
  (default `true`; MCP arg `enforce_utility_horizon`, default on). Explicit
  as-of / history surfaces remain the path to past-horizon rows.
- Post-filter placement mirrors #1000's `type_filter` retain: applied at
  the top of `recall()`, so it covers every mode (FTS5, dense, hybrid,
  fused).

## Surfaces

- `perseus_vault_decay_audit` (Ops, read-only): the profile table plus
  per-type population aggregates — count, mean decay score, mean age days,
  and `past_horizon_count` (exactly the rows the default gate excludes).
- No schema change: profiles are code-level constants; ages derive from
  `entities.created_at_unix_ms`.

## Tests

`src/temporal_decay.rs` — profile-table determinism, horizon boundary
arithmetic (30-day episodic: 30d within, 31d past; durable never past),
and an end-to-end gate test: a 40-day-old episodic is excluded with the
gate on and surfaces with the gate off, while a 400-day-old preference
surfaces in both modes; audit attributes the excluded row.
