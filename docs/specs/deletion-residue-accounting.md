# Deletion-Residue Accounting (#990)

Status: implemented (reference: PR pending) · Issue: perseus-vault #990 ·
Borrowed-from: MythologIQ-Labs-LLC/agent-memory `residue.py` (Apache-2.0),
verified independently at their `f07758c` (18/18 driver checks) before
adoption.

## Problem

`purge` (permanent deletion of archived rows) and `forget` (soft archive)
classify nothing about *what derived state survives a deletion*. Derived
surfaces — the pre-quantization embedding snapshot, declared projection
basis, learned-artifact bindings — are handled ad hoc: some are cleaned
(`artifact_bindings` were already revoked by #876), some are retained
forever without a receipt (`entities_embedding_snapshot` rows for purged
entities were never touched), and none are verified.

A deletion that leaves recoverable content it did not report is a failed
deletion however much it removed. The undeclared-residual cell must be
empty — and the way to know it is empty is to re-derive it independently,
never to ask the purge whether it finished.

## The four-way partition

Everything derived from a purged source is classified into one of:

| Cell | Meaning | In the Vault today |
|---|---|---|
| `purged` | demonstrated removed | entity rows, FTS rows, history rows (incl. FTS), embedding snapshot rows, projection-basis rows, revoked artifact bindings |
| `declared_residual_controlled` | survives, reported, within reach | journal rows redacted in place (audit hash chain preserved — #398) |
| `declared_residual_uncontrollable` | survives, reported, outside reach | none (no export tracking; documented as out of scope) |
| `undeclared_residual` | survives and was not reported | **must be 0** — hard gate, fail-closed |

Two honesty constraints carry over from the source doctrine:

1. **Unknown is not a fourth bucket.** State whose derivation cannot be
   enumerated is declared (e.g. the bulk quantization snapshot is a declared
   `embedding_snapshot` projection), never omitted.
2. **Traversal completeness is itself the measurement.** The sweep
   re-derives orphan state from the retained tables over *every* row; a
   purge that forgot a surface is caught rather than believed.

## Surfaces and their basis

New table `projection_basis` (SCHEMA_VERSION 39) declares what a tier-3
projection was built from:

```sql
projection_kind TEXT NOT NULL,            -- 'embedding' | 'embedding_snapshot' | ...
projection_id   TEXT NOT NULL,            -- entity id, or a bulk marker
source_entity_id TEXT NOT NULL DEFAULT '',-- '' = bulk projection (sweep-exempt)
source_digest   TEXT NOT NULL DEFAULT '', -- sha256 of the plaintext the vector was computed from
source_recorded_at_unix_ms INTEGER, built_at_unix_ms INTEGER,
content_class, transform, reachable      -- vocabulary as in the doctrine
```

- Every embedding write (both store paths, incl. the guarded worker path)
  upserts `('embedding', id, id, digest, recorded_at, now, ...)` — the
  basis is the entity's plaintext at build time (FTS row is the canonical
  plaintext; `entities.body_json` may be ciphertext).
- The quantization snapshot is a *bulk* projection: `source_entity_id=''`
  makes it exempt from the orphan sweep (its rows are maintained
  individually by purge instead).

## Independent sweep

`Database::residue_sweep()` (also `perseus_vault_purge {sweep_only: true}`)
re-derives undeclared residual state from the tables themselves:

1. `entities_embedding_snapshot` rows whose id is not in `entities`
   (orphan snapshot rows — the concrete pre-fix leak);
2. `projection_basis` rows with a non-empty `source_entity_id` not in
   `entities` (declared basis whose source is gone);
3. `artifact_bindings` that have source rows but whose sources are missing
   and which were never revoked (unreported residue — serve paths already
   refuse them, but the residue was unaccounted);
4. informational only: non-redacted journal rows referencing missing
   entities (the erase path may legitimately leave some; **never** part of
   the gate).

`undeclared_total = (1)+(2)+(3)`; `hard_gate_passed = undeclared_total == 0`.

## Hard gate

`purge` runs the sweep on its own transaction **after** its deletions and
**before** commit. If any undeclared residue exists (from any cause — a
surface the purge forgot, or pre-existing stale state), the purge refuses
and the transaction rolls back. `dry_run` previews the full partition and
the gate status; `sweep_only` runs just the sweep. This is fail-closed by
design: the operator resolves the orphan (or declares it), then purges.

Deletion dominates correction: superseded versions retained in
`entity_history` for reconstructability are removed with their source
(existing #398 behavior), now counted in the `purged` cell.

## Tool surface

`perseus_vault_purge` gains `sweep_only` (bool, default false). The report
gains `embeddings_snapshot_deleted`, `projection_basis_deleted`, and
`residue` — the four-way partition with counts, the undeclared item list,
and `hard_gate_passed`.

## Corpus

Three deletion-residue traces added to the shared authority-trace corpus
(`benchmark/security/traces/authority_traces.json`, generator:
`generate_authority_traces.py`): `residue-undeclared-snapshot-orphan`,
`residue-compliant-purge-partition`, `residue-deletion-dominates-correction`.
Each carries a `residue_model` contract (expected undeclared total, gate,
purged/declared classes). The Rust runner test builds each trace's state
through public surfaces and asserts the model reproduces the contract —
runner wiring for the shared corpus so other implementations can run the
identical cases.

## Verification

- `cargo check` clean; full suite green (previous count + 7 new tests).
- New tests: snapshot/basis cleaned on purge; sweep catches orphan snapshot
  + basis rows; purge fails closed on undeclared residue and rolls back;
  dry-run previews without deleting; journal redaction counted as declared
  controlled; sweep-only tool mode; corpus runner (3 traces).
