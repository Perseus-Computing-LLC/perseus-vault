# Shared-memory scale benchmark

`run.py` exercises concurrent, isolated MCP clients against one SQLite-backed Perseus Vault. It measures both serving correctness and retrieval latency under shared-memory pressure.

```bash
cargo build --locked --no-default-features
python3 benchmark/shared_scale/run.py --bin target/debug/perseus-vault --agents 16
```

## Metrics

- `retrieval_quality`: fraction of agents that retrieve the workspace-shared fact.
- `privacy_quality`: fraction of agents prevented from retrieving the writer's private fact.
- `latency.mean_ms`, `latency.p95_ms`, `latency.max_ms`: latency for the shared-fact recall call.
- `passed`: requires 100% retrieval and privacy correctness plus p95 latency at or below the configured budget (default: 250 ms).

## Tested assumptions and limits

- The default scale is **16 concurrent MCP client processes** sharing one SQLite vault and one workspace.
- The harness isolates each client by MCP identity and validates a private writer record does not leak to readers.
- It deliberately does **not** claim distributed or multi-host load coverage; host-calibrated latency values should not be compared across machines without an equivalent same-hardware baseline.
- Increase `--agents` and tune `--latency-budget-ms` for a controlled local capacity investigation. The output reports the assumptions so artifacts retain their provenance.

The benchmark is offline and only uses a fresh temporary database.
