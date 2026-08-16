# State-to-Draft Audit — Implicit Stale-Dependency Repair (#1093)

Borrow: STALE / StateAuditor (arXiv:2608.01619). State-table entries are
written against assumptions about the entities they reference. When a
dependency drifts silently (an entity consolidated away, a cohort pruned,
a cached count stale), the entry still parses and serves — but its premise
is gone. Serving it quietly is a lie; deleting it destroys evidence.

## Dependency rules (deterministic, per family)

| family                    | dependency                          | stale when                                    |
|---------------------------|-------------------------------------|-----------------------------------------------|
| `sleep_proposal.*`        | `entity_a`, `entity_b`              | either referenced entity no longer exists     |
| `shadow_promote_last`     | `ids`                               | any promoted id no longer exists              |
| `skill.exp.stats.<id>`    | `skill.exp.<id>.*` paths            | stats `n` ≠ logged path count                 |
| any value with `snapshot_entity_count` | live entities table    | embedded count ≠ live active count            |

Findings name the key and the exact broken dependency — never a
heuristic score. Already-demoted entries (`status: "stale"`) are skipped,
so repair is idempotent.

## State-to-draft repair

1. **Draft-copy** — the stale value is preserved verbatim under
   `state_draft.<receipt>`, wrapped with source key, demotion time, and
   reason.
2. **Demotion** — the live key is rewritten shape-compatibly: sleep
   proposals keep their `SleepProposal` shape with `status: "stale"` (the
   review lane still renders them, clearly marked); other entries get a
   stale marker object.
3. **Receipt** — every repair journals `state_stale_repaired`
   (key + reason + draft reference).

`perseus_vault_state_audit {dry_run}` (Ops) reports or repairs; dry-run
performs the identical evaluation with zero writes.

## Tests

`src/state_auditor.rs` — per-rule findings (missing entity, stats drift,
snapshot drift, shadow ids), and the end-to-end cycle: dry-run is pure;
a real run drafts originals verbatim, demotes shape-compatibly, anchors
three receipts, and a second pass finds zero stale (idempotent repair).
