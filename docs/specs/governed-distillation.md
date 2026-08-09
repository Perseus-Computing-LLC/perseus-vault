# Governed Distillation (#876)

Status: **implemented (v1)** · Scope: learned-memory training/compaction governance

## Problem

Learned memory (trained weights, distilled cartridges, compacted embeddings) is
the one write path that can silently outlive its sources: a fact is corrected
or erased, and the trained artifact that absorbed it keeps serving the stale
or erased knowledge. The vault needs a governance surface that makes every
distillation action **auditable, source-bound, and revocable** without holding
the training run itself.

## Model

### Capability: `learned_memory`

A new capability in the existing authority-manifest vocabulary
(`authority_set` → `allowed_capabilities`). Nothing special about the name —
the manifest machinery already gates it — but the **registration gate** below
makes it load-bearing:

> **No `learned_memory` manifest entry → `action_intent` with that capability
> is denied (self-explanatory denial, #768) → no receipt → no registration.**

### Action flow (existing AAR control plane, reused)

```
authority_set(manifest: [learned_memory])
  → action_intent(capability="learned_memory", scope_anchor, external_ref,
                  intent_hash, resource_constraints_json)
  → [approval if manifest requires it]
  → action_lease_acquire / action_lease_release (single holder)
  → action_complete(outcome="executed", outcome_hash = sha256(artifact bytes))
  → action_receipt_get → receipt replay
```

`resource_constraints_json` is the provider-action surface: corpus scope
(categories/keys), provider identity, retention bound, and compute budget are
recorded there by the training runner and become part of the receipt.

### Registration gate: `perseus_vault_learned_artifact_register`

Fail-closed — every refusal is an error, never a silent fallback:

1. `action_id` must resolve to a **completed** receipt (terminal status
   `action_executed`).
2. Receipt capability must be **`learned_memory`** (a `git_push` receipt is refused).
3. Receipt actor + workspace must match the registration's `agent_id` + `workspace_hash`.
4. A non-empty receipt `outcome_hash` must equal the artifact's SHA-256.
5. Every `source_entities` (category, key) must resolve in the workspace.

On success the artifact is stored via the generic immutable-artifact path
(`derived` representation, `derivation_kind = "learned_memory"`) and every
source is snapshotted **hash-only** into `learned_artifact_sources`:

| column | meaning |
|---|---|
| `binding_id` | artifact_bindings FK (CASCADE) |
| `entity_id` | source entity id |
| `category` / `key` | source coordinates |
| `value_sha256` | normalized body digest (`rejected_value_digest`) |
| `recorded_at_unix_ms` | source `recorded_at` at snapshot time |

The registration response carries `evidence` with the source snapshot and a
receipt-replay block, so a verifier can re-derive `artifact → sources →
workspace` without trusting the caller.

### Lifecycle: revocation and staleness

| event | flag | serve behavior | journal evidence |
|---|---|---|---|
| source **erased** (`perseus_vault_erase`, #868) | `revoked_at_unix_ms` + `revocation_reason` | **refused** (fail-closed, all bindings revoked ⇒ not-found) | `artifact_revoked` (entity id + binding count, hash-only) |
| source **purged** (`perseus_vault_purge`) | same | refused | `artifact_revoked` (per source) |
| source **superseded** (`perseus_vault_supersede`) | `stale_at_unix_ms` | still serveable, flag visible in manifest | `artifact_stale` (entity id + binding count) |

The purge report exposes `artifact_bindings_revoked` (dry-run preview and real
count). Stale flags are the retraining trigger: an operator (or a later
automation) re-runs distillation and registers the fresh artifact.

### Serve path

`artifact_resolve_visible` (the single choke point behind `artifact_manifest`,
`artifact_excerpt`, `artifact_verify_value`) drops revoked bindings before
visibility filtering; an artifact with **no live binding left is
indistinguishable from not-found**. Stale bindings stay serveable and the
manifest carries `stale_at_unix_ms` + `revoked_at_unix_ms` +
`revocation_reason` on every binding.

## Data boundaries

- `learned_artifact_sources` holds ids + digests + timestamps only — **no
  content**, consistent with the hash-only audit-evidence policy (#866).
- Journal events (`artifact_revoked` / `artifact_stale`) are aggregate +
  entity-id; journal redaction rules continue to apply.
- Cross-workspace isolation: registration and revocation are workspace-scoped;
  a source in workspace A never revokes a binding in workspace B.

## Schema

- v29: `artifact_bindings.revoked_at_unix_ms`,
  `artifact_bindings.stale_at_unix_ms`,
  `artifact_bindings.revocation_reason`,
  `learned_artifact_sources` (FK CASCADE, `idx_learned_sources_entity`).
- Migration path is idempotent (`ensure_column` / `IF NOT EXISTS`), additive,
  backfill-free.

## Tests

- receipt gate: no receipt / wrong capability / outcome mismatch / actor
  mismatch / missing source — all refused with named errors
- happy path: intent → complete → register; source digests bind; manifest
  serves; receipt replay matches
- purge revocation: dry-run count, real count, fail-closed serve, journal
  evidence
- supersede staleness: flag set, still serveable, journal evidence,
  idempotent
