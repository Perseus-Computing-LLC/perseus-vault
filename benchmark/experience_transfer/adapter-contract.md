# Adapter contract: verified-experience-transfer/v1

Status: frozen for provider-free review

## Purpose

An adapter consumes one `agent_view` from the shared synthetic corpus and returns a
bounded decision record. The corpus is shared; implementation code, private prompts,
provider responses, and storage backends are not shared. Adapters must not receive or
infer `evaluation.expected_decision_class`, `evaluation.expected_reason_code`, or the
case-level label commitment during evaluation.

## Input

The input is exactly `case["agent_view"]` from `benchmark/experience_transfer/corpus/corpus.json`:

```json
{
  "case_id": "vet-01-fresh-runbook",
  "task_b": {"task_id": "task-b-01", "goal_code": "...", "requested_operation": "reuse_experience", "world_state_id": "...", "world_version": 2, "world_state_hash": "<sha256>"},
  "world": {"world_state_id": "...", "version": 2, "status": "current", "facts_code": "...", "state_hash": "<sha256>"},
  "experiences": [{"experience_id": "...", "memory_id": "...", "approach_type": "worked", "approach_outcome": "verified_success", "validity_status": "current", "world_state_hash": "<sha256>", "source": {}, "evidence": [], "lineage": {}, "scope": "workspace-cinder"}],
  "authority": {},
  "revalidation": {},
  "controls": {}
}
```

The complete field contract is enforced by `benchmark/experience_transfer/common.py` and summarized in
`benchmark/experience_transfer/schemas/adapter-contract.schema.json`.

## Output

```json
{
  "adapter": "perseus_vault_governed",
  "case_id": "vet-01-fresh-runbook",
  "decision": "reuse|reject|abstain|block",
  "reason_code": "bounded_identifier",
  "revalidated": true,
  "provenance_validated": true,
  "authority_checked": true,
  "unsafe_reuse": false,
  "selected_memory_count": 1,
  "transition_steps": 4
}
```

`revalidated` means that a required check was performed; it does not imply the check
passed. `unsafe_reuse` is true only when the adapter reuses an experience despite a
risk condition it did not clear. An adapter must raise a contract error on malformed
input or output; it must not convert tampering into a safe-looking abstention.

## Required adapter metadata

Each implementation declares `name`, `version`, `status`, and a human-readable
`reason`. `status` is `pass` only for an executed provider-free adapter contract;
`not_measured` is required for an unexecuted external implementation. The external
adapter must not be represented by zero quality scores.

## Reference semantics

The governed adapter checks, in order: scope/split-brain/contradiction; current
authority; derived lineage; evidence usability; stale world and revalidation;
current/superseding experience; evidence sufficiency; then reuse. It preserves
historical/superseded records for provenance but never treats them as current without
a valid replacement or revalidation.

## Instrumentation overlay

Ledger/provenance capture is orthogonal to semantic adapter results. A future run may
record receipt hashes, source-selection hashes, and authority/action hashes, but those
fields must not change the model-visible context unless the run declares a separate
instrumentation arm. Public projections contain hashes and counters, never bodies.

## External collaboration boundary

A CogniCore or other external adapter should implement this contract against the same
corpus and publish a sanitized report with its own implementation/version, world and
corpus commitments, per-case decisions, usage telemetry if provider-backed, and
limitations. It should not copy Perseus code or data. The current package does not
contact or execute any external implementation.
