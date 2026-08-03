# Indirect memory-poisoning admission v1

Status: bounded defense/measurement slice for #821

`src/trust_admission.rs` evaluates memory-write metadata without receiving the
raw memory body. A caller supplies only a record digest, source identity,
workspace/authorization scope, ingestion channel, trust class, temporal fields,
relevance score, and boolean screening results from an upstream scanner.

## Outcomes

- `admitted`: trusted evidence may be durable; only an authorized authoritative
  source becomes authoritative.
- `quarantined`: untrusted or instruction-bearing material is retained as
  non-authoritative evidence and cannot activate on a later benign query.
- `suppressed`: insufficient task relevance; it cannot enter an active context.
- `escalated`: contradiction with an authoritative record requires review.
- `abstained`: missing/invalid scope means the gate fails closed.
- `revoked`: operator rollback changes activation state while preserving the
  original record digest and a hash-only revocation digest.

Every decision carries a canonical SHA-256 decision digest and bounded reason
codes. `can_activate()` additionally requires an admitted authoritative durable
record, exact workspace scope, and minimum query relevance. This prevents a
quarantined two-phase injection from becoming active merely because a later
query is benign.

This is not a universal security guarantee. It is a bounded admission and
measurement contract; deployments still need authenticated source identity,
independent screening, operator review, and defense-in-depth at tool/action
boundaries.
