# Court of record (operator guide)

The court of record turns consistency findings into **enforced, audited
guards** — without ever letting the model mutate on its own. Model
confidence is evidence; only an operator ruling compiles a guard.

## When to use

- After `perseus_vault_consistency_audit` (or the operator review queue)
  surfaces contradiction pairs you want settled.
- When a deprecated fact's successor is missing (supersession lag).
- Whenever you want the winner of a dispute to stop surfacing in recall.

## Flow

1. **Audit (read-only):** `perseus_vault_consistency_audit
   {"category": "facts"}`. Each finding carries a deterministic
   recommendation — importance → source authority (curated > capture >
   agent > web_gap_fill) → recency → id — with `decided_by` naming the
   rung. Already-ruled pairs show `already_ruled` instead and are never
   re-recommended.
2. **Rule:** `perseus_vault_audit_ruling {"action": "accept", ...}` compiles
   the recommended winner into the existing supersede guard (winner→loser
   `supersedes` link, loser valid-period closed, loser status `deprecated`).
   `override` compiles an explicit winner you name; `reverse` reopens a
   ruled pair (the compiled guard remains — re-assert to re-litigate).
3. **Verify:** the response returns the ruling record + `supersede_receipt`
   (the guard evidence). `perseus_vault_consistency_audit` will now show the
   pair as `already_ruled`.

## Semantics

- **Idempotent:** re-ruling the same pair with the same winner returns the
  existing ruling unchanged. A different winner while a ruling is active is
  refused — `reverse` first.
- **Immutable until reversed:** `UNIQUE(pair_fingerprint, status)`.
- **Audited:** every ruling is journaled (`court_ruling_set` /
  `court_ruling_reversed`) with the deciding agent.
- **Bounded projection:** rulings store ids/fingerprints/status only — no
  entity bodies are copied into the court table.
- **Read-only audit invariant:** the audit never mutates (verified by the
  test suite's entity-count check).

## Storage

`schema_version` 38: `court_rulings` table (pair fingerprint, winner/loser
ids, ruling kind, rationale, status, decided_by, supersede_receipt,
reversal metadata). Backwards compatible — existing databases migrate on
open.
