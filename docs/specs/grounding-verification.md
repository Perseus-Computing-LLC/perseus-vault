# Deterministic grounding verification (perseus-vault #1034)

Status: implemented (schema v48, `perseus_vault_grounding_admit` +
`perseus_vault_grounding_reconcile`; fingerprint module `src/grounding.rs`).

Source: mex-memory/mex (MIT, 1.4k stars, verified locally 2026-08-14) — the
grounding checker (bodyHash at grounding time, drift diff on rebuild, MinHash
reconcile with HI/LO thresholds) and its fail-closed authoring rule.

## Problem

Rendered evidence and grounded facts go stale silently when the underlying
files/symbols change. Nothing mechanically surfaces "the thing this fact
points at moved or changed" without an LLM pass.

## Design

### Fingerprint

- K=64 seeded-sha256 trigram MinHash + neighbor set (unique trigram
  shingles), captured at admission — deterministic, zero LLM, reproducible.
  Fixed derivation seed; fingerprints are self-describing
  (`seed=<hex>;k=64;<h1>:<h2>:…`) and parseable for cross-implementation
  comparison.
- Baseline digest = sha256 of the admitted content — the cheap
  identical-content fast path.

### Admission (`perseus_vault_grounding_admit`)

- The agent supplies the grounded content; the vault never fetches.
- Fail-closed authoring rule (verbatim from mex): *"If trustworthy ground
  facts are unavailable, stop and report it. Never invent node ids or
  fingerprints."* — nonexistent entity, short content, or unbounded bodies
  are refused with stable reasons.
- Re-admission refreshes the baseline and appends a provenance event —
  never silent last-write-wins.

### Reconcile (`perseus_vault_grounding_reconcile`)

For each admitted grounding, diff the current-content scan (target_ref +
content pairs) against the baseline:

| Condition | Outcome |
|---|---|
| current digest == baseline | `ok` (clears any previous flag) |
| exists-but-changed, no moved candidate | `drift` (GROUNDING_DRIFT) |
| score ≥ HI against exactly one candidate | `moved` — anchor auto-rewritten (target_ref + fingerprint + baseline migrated) with a supersede-style provenance trail (journaled `grounding_moved`; never silent) |
| score < LO, no candidate | `gone` — flagged for operator review (journaled `grounding_gone`; never auto-deleted) |
| in-band (LO ≤ score < HI) or ≥2 candidates | `ambiguous` — candidates surfaced for the operator review queue |

Reconcile score = 0.7×minhashJaccard + 0.3×neighborOverlap (mex's constants),
HI 0.85 / LO 0.55.

### Pitfall designed around (found in mex)

mex migrates reference forms non-atomically (frontmatter grounding before
inline anchors, deleting the shared baseline in between) leaving the second
form stale. The vault keeps ONE canonical reference form per grounding row
(target_ref + fingerprint + baseline) and rewrites all three in a single
UPDATE inside the same reconcile pass, so no partial-migration state exists.

## Data model (v48)

`grounding_fingerprints` — one row per (workspace, entity, target_ref):
fingerprint_hex, neighbor_count, neighbors_json (bounded at 256),
baseline_digest, status (ok/drift/moved→ok/gone/ambiguous),
candidates_json, provenance_json (append-only trail), reviewed_at_unix_ms.

## Tests

`grounding_admit_is_fail_closed_on_missing_entity_and_short_content`,
`grounding_reconcile_detects_drift_moved_gone_ambiguous` (live replay:
ok → drift → moved with provenance → gone → ambiguous with candidate
surface), plus the `grounding` module unit tests (determinism, jaccard
estimation, HI/LO band behavior).
