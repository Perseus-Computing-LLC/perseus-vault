# Cross-product served-memory acceptance contract

**Producer:** `perseus_vault_recall` is the canonical public producer name. Legacy `perseus_vault_*` and `perseus_vault_*` names remain compatibility aliases during v2, but must not appear as the producer of new acceptance evidence.

## Fixture

`tests/fixtures/cross_product_served_memory_contract.json` is a shareable projection for the Perseus → Vault → Ledger acceptance harness. It includes only:

- served-memory identity, scope, promotion provenance, origin kind, and external reference identifiers;
- `why_served` explanation fields; and
- action/authority/approval identifiers plus a SHA-256 outcome commitment.

It deliberately excludes raw memory content, `body_json`, prompts, secrets, and rendered-context text. Perseus hashes its deterministic render and Ledger commits the corresponding hash-only projection in an evidence receipt.

## Invariants

1. `why_served.memory_class` equals the item category.
2. `why_served.promotion_state` equals `promotion_transition.to_state`.
3. `why_served.source_evidence_ids` includes `promoted_from.id`.
4. `why_served.promoted_scope` equals the item workspace scope.
5. The action outcome commitment is a 64-character hexadecimal SHA-256 digest.

The fixture is intentionally deterministic and safe for cross-repository test consumption; it is not a live authorization or approval mechanism.
