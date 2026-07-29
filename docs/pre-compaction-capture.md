# Pre-compaction capture pipeline

This document defines the ordering guarantee implemented for [#780](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/780).

## Guarantee

When `perseus_vault_autocohere` receives `capture_text`, Vault executes this sequence:

```text
1. Distill `capture_text` with the normal capture pipeline.
2. Persist every resulting durable note through the normal entity write path.
3. Only after capture succeeds, run cohere → decay → compact → consolidate → history retention.
```

If the pre-compaction capture stage fails, the grooming pass aborts. It does **not** continue into a lifecycle step that could archive, summarize, prune, or otherwise compress the source context.

The capture output is reported under `precompact_capture`:

- `stage: "completed"` — durable capture ran before lifecycle work; the nested `report` is the normal capture result.
- `stage: "skipped"` — no `capture_text` was supplied, so no raw buffer was available to capture.

`dry_run: true` propagates to capture and every subsequent lifecycle stage. A preview writes no captured entities and performs no archival or compaction mutations.

## Invocation

```json
{
  "capture_text": "## Decision\nUse blue-green deployment so rollback remains safe.",
  "capture_workspace_hash": "workspace-id",
  "capture_agent_id": "agent-id",
  "capture_max_entities": 10
}
```

Call this through `perseus_vault_autocohere`. Legacy `mimir_autocohere` and `mimir_capture` aliases remain callable during the v2 compatibility window. The pre-compaction capture remains bounded by the ordinary capture cap and near-duplicate merging rules.

## Scope and non-goals

- The pipeline captures only the caller-supplied buffer. Vault cannot infer raw context that a host never provides.
- Existing direct `perseus_vault_compact`, `perseus_vault_prune`, and history-retention calls remain explicit low-level operations; hosts that hold raw session context should use the combined autocohere path with `capture_text`.
- Captured notes are ordinary durable entities with `source: "capture"`, evidence-friendly history, workspace scope, and normal retention behavior.
