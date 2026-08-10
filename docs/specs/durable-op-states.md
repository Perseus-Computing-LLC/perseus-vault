# Durable operation states — shared long-running run contract (#871)

## Problem

Maintenance (cohere/decay/compact/consolidate), embedding backfill, export/import,
and reindex operations are long-running and currently report only at the end —
all-or-nothing. A crash, timeout, or empty output looks like "nothing happened"
(or worse, like success). There is no persisted record of what ran, how far it
got, what partially succeeded, or why it stopped.

Grounded in:

- AIMAOS `core/jobs.py` — SQLite-persisted job metadata, `interrupt_unfinished_jobs()`
  on startup, `redact_sensitive()` on stored errors, `prune_runtime_history(days)`.
- Helix Agent Lab `server.py` — run records with `running|passed|failed|cancelled|
  failed_to_start`, restart-orphan detection (`unknown_after_server_restart`),
  artifact directories, exit codes.
- Vault #864 (reliability contract for reads: fresh/partial/timeout/unavailable/
  empty/stale, bounded deadlines, per-item retry, partial-success artifacts).

## Contract

### Run states

```
queued → running → completed | failed | cancelled | interrupted
queued → cancelled | interrupted | failed_to_start
```

- `queued` — accepted, not started.
- `running` — work in progress.
- `completed` — terminal; all items done (may be 0 — an explicit no-op IS a
  successful completion, never conflated with failure).
- `failed` — terminal; unrecoverable error (may carry `partial`).
- `cancelled` — terminal; operator/user cancellation.
- `interrupted` — terminal; process restarted (or queue lost) while in flight.
  **Mark-only, never auto-resumed.** Resume happens only through an explicit
  `op_run_retry` (idempotent policy below).
- `failed_to_start` — terminal; the operation could not launch (bad input,
  missing provider, spawn error). Distinct from `failed` (work never began).

Orthogonal flags: `partial` (some items done, not all), `timeout` (bounded
deadline exceeded), `stale` (operator-facing liveness marker for observability).
Flags are set alongside a state; they never replace it.

Terminal states accept no further transitions. Retry forks a NEW run.

### Run record (`op_runs`, schema v35)

Bounded status + scope + input digest + progress counters + artifact/receipt
linkage + error class + sanitized detail:

- `id` (`opr-…`), `op_type` (`consolidate|embed_flush|export|import|decay|
  maintain|reindex|cohere|compact|custom`), `state`, `partial/timeout/stale`
  flags, `scope` (workspace hash or empty = global), `input_digest` (sha256 of
  the input reference set — idempotency anchor), progress counters
  (`items_total/done/failed/unattempted`), `error_class`, `error_detail`
  (**sanitized at write time, bounded length**), `receipt` (terminal receipt
  linkage: journal event id or artifact ref), `retry_count`, `max_retries`,
  `parent_run_id` (retry chain), `created_by`, timestamps.

### Per-item receipts (`op_run_items`)

`item_ref` (entity id / file path / batch ordinal), `item_digest`, per-item
state (`queued|running|completed|failed|cancelled|interrupted`),
`receipt_ref` (per-item receipt linkage), `error_class`, sanitized
`error_detail`, `retry_count`, timestamps. `UNIQUE(run_id, item_ref)`.

Partial success preserves completed item receipts and identifies failed and
unattempted items (`items_done/items_failed/items_unattempted` counters plus
per-item rows).

### Retry policy (bounded, scoped, idempotent)

`op_run_retry(run_id)`:

1. Refused fail-closed if `retry_count >= max_retries` (`retry_exhausted`).
2. Refused if the run has no recoverable items (all completed/cancelled with
   nothing failed/unattempted → `nothing_to_retry`).
3. Forks a NEW child run (`parent_run_id` = source id, `retry_count` =
   parent + 1, same `op_type`/`scope`/`input_digest`/`max_retries`).
4. Re-queues ONLY `failed | cancelled | interrupted | queued | running`
   (unattempted) items; `completed` items are carried into the child as
   completed WITH their `receipt_ref` copied — never re-executed, so retry
   cannot duplicate writes or receipts.

### Restart recovery

`Database::open` runs `recover_op_runs()`: every `op_runs` row in
`queued|running` and its in-flight items become `interrupted` (mark-only —
never auto-resumed; resume happens only through an explicit `op_run_retry`).

### Secrets and retention

- `error_detail` is sanitized at write time: `sk-…`, `KEY=value`/`token=…`
  assignments, `Bearer …`, 32+ char hex/base64 blobs → `[REDACTED]`; length
  capped (500 chars, truncation marker). Stored output never contains raw
  provider payloads.
- `op_run_prune(retention_days)` (default `PERSEUS_VAULT_OP_RETENTION_DAYS`,
  30) deletes terminal runs older than the bound plus their items. In-flight
  runs are never pruned. `maintain` runs a prune pass each cycle.

## Surface

- **MCP tools** (110 → 115):
  `perseus_vault_op_run` (actions `begin|start|progress|complete|fail|
  failed_to_start|cancel|timeout|item_add|item_start|item_complete|
  item_fail|item_cancel`),
  `perseus_vault_op_run_list` (filter `state`/`op_type`, bounded `limit`),
  `perseus_vault_op_run_get` (run + items),
  `perseus_vault_op_run_retry`,
  `perseus_vault_op_run_prune`.
- **CLI**: `perseus-vault op-runs list|show|retry|prune` (scheduler + crash
  observability).

## Integration

| Operation | Run wrap | Progress / receipts |
|---|---|---|
| `consolidate` | begin/start, complete/fail | run-level counters; response gains `op_run_id`/`op_run_state` |
| `vault_export` / `vault_import` | begin/start, complete/fail | per-error item receipts (≤50, `error-N`) identify failed artifacts |
| `embed` (store-wide quant reindex / snapshot restore / drop) | begin/start, complete/fail | run-level; response gains `op_run_id`/`op_run_state` |
| `decay_tick` (tool + CLI) | begin/start, complete/fail | receipt carries `DecayReport` counters |
| CLI `maintain` | begin/start, complete/fail | per-phase progress (2 phases: autocohere, maintenance) |
| `reindex` (tool + CLI) | begin/start, complete/fail | receipt carries `reindexed=N` |

## Acceptance coverage

- [x] Machine-readable terminal state + bounded progress (MCP tools + CLI)
- [x] Restart recovery marks in-flight work `interrupted` (mark-only; explicit
      retry policy)
- [x] Partial success preserves completed item receipts, identifies
      failed/unattempted
- [x] Retry bounded, scoped, no duplicate writes/receipts (completed items
      carried with receipts)
- [x] Status/error outputs exclude secrets; retention prune
- [x] Tests: crash, timeout, queue saturation, malformed provider output,
      retry exhaustion, no-op empty input, illegal transitions, scoped retry,
      redaction, retention, CLI integration
