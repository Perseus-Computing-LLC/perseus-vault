# Dependency-Guided Rollback Repair (#1084)

Status: implemented · Source: arXiv:2608.10502 ("From Faulty Memories to
Corrected Actions: Dependency-Guided Rollback Repair for Memory-Augmented
Agents")

## Problem

Deleting or revising a faulty memory leaves already-propagated claims,
actions, and derived memories active. Perseus #1029 (supersession impact
index) identified this effect-continuity gap; this repair supplies the
repair-action semantics on top.

## Mechanism

`perseus_vault_rollback_repair` (Ops):

1. **Diagnose** — the caller supplies the diagnosed faulty entity ids
   (drift-check findings, operator review, or a red-team verdict). Unknown
   ids fail closed.
2. **Dependency graph** — built from existing runtime provenance, not new
   instrumentation:
   - entity links citing a faulty id (including the #1064 typed edges),
   - supersession edges (an entity replaced BY a faulty successor carries
     the pre-fault state),
   - journal tool-call records per faulty id (action evidence counts).
3. **Classify** — the paper's preservation gate: a dependent survives iff
   it retains at least one NON-faulty source edge (independent trusted
   support); otherwise its derived state is unsupported.
4. **Preserve** — surviving dependents keep their content; edges to faulty
   sources are severed (and recorded for reversal).
5. **Deactivate** — unsupported state and the faulty roots themselves are
   tombstoned: status `quarantined` (removed from the serveable set) with an
   archive reason naming the repair. **Never deleted** — 100% benign
   preservation and zero destructive writes by construction.
6. **Selective replay** — with `replay=true`, a bounded dry-run
   consolidation proposal is produced per affected category/workspace only
   (never whole-store, never auto-committed; the operator commits via the
   existing `perseus_vault_consolidate` after review).
7. **Receipts + reversibility** — every step journals a receipt
   (`rollback_repair_tombstone` / `rollback_repair_preserved` /
   `rollback_repair_reversed`), and a durable state record
   (`rollback_repair.<id>`) holds the full plan — prior statuses and severed
   edges — so the repair is auditable and fully reversible:
   `perseus_vault_rollback_repair {reverse_repair_id}` restores statuses and
   re-links edges, then journals the reversal.

## Success criteria vs implementation

- Recovery ≥ 85% on the paper's 150-case benchmark: the paper's corpus and
  harness are not redistributable/reproducible locally, so this criterion
  remains for the benchmark track; the mechanism-level contract (preserve
  independent support, tombstone unsupported, never delete) is pinned by the
  in-suite tests below.
- Benign-memory preservation 100% + zero deletes: asserted by test — the
  benign row's status, links, and body are untouched; the repair performs
  UPDATEs to status/archive_reason/links only.
- Claim invalidation F1 ≥ 0.65 on the adapted stress set: mechanism test
  asserts exactly the invalidation boundary (unsupported claims leave the
  serveable set; supported claims stay with their retained evidence).
- Every repair step receipt-anchored: asserted — 3 receipts for the
  canonical fixture (2 tombstones + 1 preserved) plus reversal receipt.

## Tests

`src/rollback_repair.rs` — pure classification/edge-severing tests plus DB
integration: end-to-end preserve/tombstone/reverse fixture, dry-run purity,
fail-closed on unknown ids, and the supersession self-support case.

## Notes

- Seeded fixture bodies are deliberately lexically distinct: the write
  interference gate (#874) quarantines trigram-similar writes, which would
  otherwise collapse a synthetic fixture.
- Agent-source writes land with status `proposed` (admission flow); repair
  restores whatever status a row had before, not a hardcoded value.
