# Task/action lineage v1

This contract is the opt-in extension to the existing AAR `action_intent` path
for issue #1134. It is deliberately separate from ordinary cross-session memory
handoff: omission of `lineage` means no task-level authorization state is
inherited.

## Request

The optional `lineage` object on `perseus_vault_action_intent` has a closed
schema:

- `schema_version`: integer `1`;
- `transition`: `new_authorization` or `continue`;
- `action_class`: a trusted taxonomy class (`read`, `external_send`, `write`,
  `delete`, or `other`); unknown capability mappings fail closed;
- `budget_cost` and `impact_units`: bounded non-negative integers;
- `continuation`: required for `continue`, optional for a deliberate successor
  authorization.

A continuation reference binds `lineage_id`, the exact `parent_head_digest`,
`continuation_state_digest`, workspace, agent identity, authority-manifest
version, and policy digest. Raw prompts, memory bodies, tool arguments,
credentials, and provider outputs are not accepted by this object.

## Durable state

`action_lineages` stores the current hash-bound state. It contains the immutable
identity and authority/policy bindings plus bounded action-class history,
automaton state, cumulative budget/impact counters, expiry, and revocation
markers. `action_lineage_transitions` is append-only history. Each transition is
linked to its AAR action ID and request/idempotency digest.

Continuation is serialized by SQLite `BEGIN IMMEDIATE` and an exact
`head_digest` plus `continuation_state_digest` compare-and-swap. A stale writer
cannot consume budget or advance the head. Same action-key retries with the
same request digest return the existing AAR action. A conflicting retry fails
closed.

## Outcomes

- `new_authorization`: a fresh lineage, or an authenticated successor reset;
- `continued`: a successful continuation;
- `denied`: a valid request rejected by resource, composition, budget, or impact
  policy; its bounded state receipt may provide the new authoritative head;
- `stale`: stale, expired, authority-mismatched, or policy-mismatched
  continuation; the current head is not advanced;
- `revoked`: a revoked continuation; the current head is not advanced.

Missing, malformed, tampered, scope-mismatched, or otherwise unverifiable state
returns an error and performs no lineage mutation. Historical transitions remain
readable and independently hash-verifiable after current-state changes.

## Ledger #266 projection

Vault does not implement Ledger receipts. The sanitized fixture beside this
document maps a Vault transition to Ledger composition-binding v1 fields:

| Vault | Ledger |
| --- | --- |
| `lineage_id` | `task_lineage_id` |
| AAR `action_id` | `authority_action_id` and `action_id` |
| authority manifest ID/version | `authority_ref` plus Vault receipt fields |
| `policy_version` | `policy_version` / `policy_hash` |
| continuation state digest | `state_hash` |
| Vault head digest | `context_head_digest` |
| `continued` / `new_authorization` | `allow` when Ledger policy admits the action |
| `denied` / `stale` / `revoked` | `deny` or `abstain`, according to Ledger policy |

This mapping is a hash-only interoperability fixture, not evidence that Vault
has evaluated Ledger trusted taxonomy or replaced Ledger prebind/evidence
levels. Ledger remains the owner of composition-verdict semantics.
