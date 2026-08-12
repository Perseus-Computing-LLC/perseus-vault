# Shadow-Import Workflow (#951)

Status: implemented on `feat/vault-951-shadow-import` (PR pending).

## Problem

`vault_import` writes straight into the live memory surface. A bad batch
(polluted corpus, wrong categories, adversarial content) is hard to review
before it lands and hard to undo afterwards.

## Design

The shadow workflow keeps the **existing workspace machinery** as the
isolation boundary — no new storage layer, no new identity rules:

```
vault_import { vault_dir, shadow_workspace: "ws-shadow-<run>" }
   └─ every imported entity is forced into the shadow workspace
      (frontmatter workspace_hash is OVERRIDDEN, never trusted)

shadow_compare { queries, live_workspace, shadow_workspace }
   └─ deterministic Fts5 recall of the SAME query set against both
      workspaces; per-query {hit, top_key} + totals {coverage,
      context_tokens_est (chars/4)}. Read-only (skip_side_effects).

shadow_promote { shadow_workspace, target_workspace, dry_run }
   └─ ONE UPDATE moves every non-archived entity into the target
      workspace; journaled in state key `shadow_promote_last`.

shadow_rollback { dry_run }
   └─ ONE UPDATE returns the journaled ids to their pre-promote
      workspace; consumes the journal.
```

## Invariants

- **Zero live writes until promote.** Import and compare touch only the
  shadow workspace; live recall is byte-identical before/during/after
  (asserted in `shadow_import_isolates_live_workspace`).
- **Idempotent by identity.** Re-running the same import creates zero new
  identities (second run reports `files_created: 0`).
- **Promote is a single statement, rollback is a single statement.** Both
  are journaled; a failed half-promote cannot exist (transactional UPDATE).
- **Rollback without a journal fails closed** ("nothing to roll back").
- **Dry runs never write** — `shadow_promote` with `dry_run: true` reports
  `would_move` from `count_entities` only.

## Determinism note

The comparison harness uses `mode: fts5` (deterministic rank; no embeddings
required) with `skip_side_effects: true`. Queries in tests must avoid
real-word tokens: `remember` stamps `provenance.reason =
"missing_admission_envelope"` into `body_json`, which IS FTS-indexed, so a
query like `"zzzz missing"` prefix-matches `missing_admission_*`. Use
nonsense tokens (`zzzzzz qqqqqq`) for no-match cases.

## Operators

```
# 1. stage
perseus_vault_vault_import {vault_dir: "...", shadow_workspace: "ws-shadow-2026-08-12"}
# 2. evaluate
perseus_vault_shadow_compare {queries: [...], live_workspace: "", shadow_workspace: "ws-shadow-2026-08-12", limit: 5}
# 3. decide (dry run first)
perseus_vault_shadow_promote {shadow_workspace: "ws-shadow-2026-08-12", target_workspace: "", dry_run: true}
perseus_vault_shadow_promote {shadow_workspace: "ws-shadow-2026-08-12", target_workspace: ""}
# 4. if wrong: undo in one op
perseus_vault_shadow_rollback {}
# 5. optional hygiene
perseus_vault_forget / archived sweep on the shadow workspace
```

## Files

- `src/db.rs` — `vault_import_inner(vault_dir, workspace_override)`,
  `vault_import_shadow`, `shadow_compare`, `shadow_promote`,
  `shadow_rollback`, `shadow_promote_journal`.
- `src/tools.rs` — `handle_vault_import` (shadow branch),
  `handle_shadow_compare`, `handle_shadow_promote`, `handle_shadow_rollback`.
- `src/mcp.rs` — 3 new tool defs + dispatch (registry 126 → 129).

## Tests

`shadow_import_isolates_live_workspace`, `shadow_import_rerun_creates_zero_new_identities`,
`shadow_compare_reports_per_query_and_totals`,
`shadow_promote_and_rollback_are_single_ops_with_recall_equivalence`,
`shadow_promote_dry_run_writes_nothing_and_rollback_without_journal_fails`.
