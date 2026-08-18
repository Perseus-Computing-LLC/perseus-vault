# Indirect memory-poisoning admission v1

Status: bounded defense/measurement slice for #821

`src/trust_admission.rs` evaluates memory-write metadata without receiving the
raw memory body. A caller supplies only a record digest, source identity,
workspace/authorization scope, ingestion channel, trust class, temporal fields,
relevance score, and boolean screening results from an upstream scanner.

## Outcomes

The evaluator retains its detailed outcomes, but the public evidence contract
also carries one stable `outcome_class`:

| Stable class | Detailed outcomes | Durable/serveable meaning |
|---|---|---|
| `save` | `admitted` | The authoritative durable head may serve the record. |
| `drop` | `suppressed` | The candidate is rejected before persistence. |
| `block` | `abstained`, `revoked` | The candidate is denied by scope or policy and cannot serve. |
| `pending_approval` | `proposed`, `quarantined`, `escalated` | Review evidence may be retained, but it is never serveable. |

`admitted` trusted evidence may be durable only when its source is authorized
and authoritative. `quarantined` material is retained as non-authoritative
evidence and cannot activate on a later benign query. `suppressed` represents
insufficient task relevance. `escalated` represents contradiction with an
authoritative record. `abstained` is the fail-closed scope/policy result, and
`revoked` is an operator rollback that preserves the original record digest and
a hash-only revocation digest.

Every decision carries a canonical SHA-256 decision digest and bounded reason
codes. `can_activate()` additionally requires an admitted authoritative durable
record, exact workspace scope, and minimum query relevance. This prevents a
pending two-phase injection from becoming active merely because a later query
is benign.

## Pending review transition

A stored `proposed` candidate with valid `pending_approval` admission evidence
is resolved only by the ops-scoped `perseus_vault_admission_decide` tool. It
requires an exact non-empty workspace, a reviewer identity, and a bounded
reason:

- `decision=approve` re-signs the evidence as `save`/`admitted`, writes the row
  through the verified writer as `active`, and appends `admission_approved`.
- `decision=reject` requires `rejection_class=drop` or `block`, re-signs the
  evidence as the corresponding terminal class (`suppressed` or `abstained`),
  archives the row as non-active, and appends `admission_rejected`.

The tool response contains identifiers, classes, status, audit-event ID, and a
reason digest; it does not return the candidate body or raw review reason.
`admission_quarantine` remains the separate sealed-retention lifecycle for
candidates rejected into the quarantine store.

## Source-event binding and digest rules

An ordinary journal row is not an admission authority. An authoritative
admission must reference a journal row whose event type is exactly
`admission_source`. The MCP journal mutation is workspace-scoped, and the
transport stamps `requesting_agent_id` onto the source row rather than trusting
the caller's `agent_id` field as the event producer.

The source row's `evaluated` object must contain the hash-bound admission
context needed by the writer:

- `record_digest`: the SHA-256 digest of the canonical candidate body;
- `source_identity`: the upstream source identity in the admission envelope;
- `workspace_hash`: the writer's workspace;
- `actor_kind` and `actor_identity`: the admission actor context.

The journal stores this evaluated object hash-only. Admission binding checks the
source event type, workspace, transport requester, and the hash of all five
fields against the candidate's admission request. A caller cannot turn an
arbitrary `decision`/`observation` journal row into authoritative evidence by
choosing matching workspace or actor labels.

The candidate body is canonical JSON before its `admission` and `provenance`
envelopes are appended. The resulting SHA-256 is the `record_digest`. When a
pending candidate is approved or rejected, the transition removes those two
envelopes in memory, recomputes the canonical digest, and refuses the
transition if it differs from the stored evidence. The review receipt is
journaled before the verified entity transition; if the receipt cannot be
written, the candidate remains proposed and is never activated.

This is not a universal security guarantee. It is a bounded admission and
measurement contract; deployments still need authenticated source identity,
independent screening, operator review, and defense-in-depth at tool/action
boundaries.
