# Runtime stage trace v1

Status: foundational implementation for #822

`src/stage_trace.rs` defines the `perseus-vault-stage-trace/v1` contract. It is a
hash-only boundary record, not a telemetry dump. The contract is intentionally
usable by retrieval, policy, mediation, tool, recovery, and receipt layers
without exposing their payloads.

## Vocabulary

The ordered stage vocabulary is:

1. `context_candidate_generation`
2. `context_selection`
3. `validation_provenance`
4. `policy_evaluation`
5. `mediation_escalation`
6. `tool_execution`
7. `recovery`
8. `receipt_persistence`

Outcomes are `in_progress`, `completed`, `degraded`, `abstained`, `failed`,
`timeout`, and `skipped`. An unfinished record is valid only with
`outcome=in_progress`; a finished record must have a nondecreasing end time.

## Fields and privacy boundary

Every trace has a schema version, trace ID, workspace hash, ordered stage records,
and an optional SHA-256 trace digest. Each stage may carry timestamps,
deadline/priority classes, outcome, model/provider identifier, token counts,
input/output SHA-256 digests, bounded reason codes, and causal links.

Causal links are restricted to `context_digest`, `decision_digest`, `action_id`,
`lease_id`, `receipt_id`, and `parent_trace_digest`. Raw prompts, memory bodies,
credentials, tool arguments, and tool results are not representable in this
schema. Stage scope must equal the trace workspace scope.

## Canonicalization and replay

The trace digest is SHA-256 over deterministic `serde_json` serialization of the
schema version, trace ID, workspace hash, and ordered stages, excluding the
`trace_digest` field itself. Digests must be lowercase 64-character SHA-256
values. `validate()` detects changed fields, invalid ordering, duplicate stages,
cross-workspace records, invalid digests, and unsupported vocabulary.

`replay_fingerprint()` excludes wall-clock timestamps and compares the ordered
semantic decisions, outcomes, token counts, content digests, causal links, and
reason codes. `validate_replay()` therefore proves equivalent replay semantics
without claiming equal latency or execution time.

This is a foundational schema/validator slice. Runtime emitters, durable export,
and Ledger/Flywheel adapters can adopt it incrementally while retaining the
existing journal and Authorized Action Receipt behavior.
