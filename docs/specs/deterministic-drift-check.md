# Deterministic drift-check → targeted-repair → verify loop (perseus-vault #1035)

Status: implemented (`perseus_vault_drift_check` + `perseus_vault_drift_repair`;
module `src/drift_check.rs`).

Source: mex-memory/mex (`mex check` / `mex sync`, MIT, verified locally
2026-08-14) — 14 deterministic checkers, health score, targeted per-file
repair prompts, re-check score delta. The vault generalizes the loop to its
evidence model and keeps detection LLM-free.

## Problem

Vault quality passes lean on LLM coherence/synthesis. A deterministic
pre-pass catches concrete drift — broken references, stale entries,
cross-file contradictions, outdated claims — at zero token cost and
reserves LLM work for the flagged subset only.

## Checkers (deterministic, zero LLM in detection)

| Checker | Severity | Repair |
|---|---|---|
| `REFERENCE_INTEGRITY` — dangling `derived_from` citations (target archived/missing) | error | mechanical: unlink (journaled) |
| `GROUNDING_STATUS` — grounding rows `gone` / `drift` / `ambiguous` (from #1034) | error / warning | mechanical: acknowledge (review flag) |
| `PATH_EXISTENCE` — grounded `file` targets missing on disk (absolute paths only; relative → info) | error | review-only |
| `CROSS_FILE_CONFLICT` — two active evidence entities asserting different values for the same keyed claim (generalized from mex's shallow version/command regex to ANY keyed scalar evidence value) | error | review-only — contradictions are never auto-resolved |
| `STALE_ENTITY` — entities untouched past the staleness window (vs `last_accessed_unix_ms`) | warning | review-only |

Health score = 100 − (10×error + 3×warning + 1×info), floored at 0.

## Loop semantics

`perseus_vault_drift_check` → issues + health score + per-issue suggested
repair → `perseus_vault_drift_repair` applies ONLY mechanical fixes to the
flagged subset, re-runs the check, and reports the before/after score delta.
Fail-closed verify leg: a repair that regresses the score is refused. The
remaining flagged subset (conflicts, staleness, missing paths) is the
bounded input to the operator review queue — exactly the "reserve LLM work
for the flagged subset" property, mirroring the bounded single-reviewer
flow.

## Relationship to quality_telemetry

Complements, never replaces: `quality_telemetry` is the machine-readable
quality signal surface; the drift loop is the deterministic detection +
repair pass that feeds it. A cheap health-score dashboard can render the
drift-check score without any LLM cost.

## Tests

`drift_check_scores_and_repair_loop_verifies` — builds a store with all five
issue classes, asserts the exact score math (100−10×4−3×1 = 57), runs the
repair leg, asserts the verify leg (after-score 77, dangling citation
actually unlinked, conflicts/staleness stay review-only, re-check is clean
for repaired classes).
