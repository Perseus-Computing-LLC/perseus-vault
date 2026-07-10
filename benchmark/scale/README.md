# Scale benchmark — 10K / 100K / 1M entities (#474)

Measures Perseus Vault at corpus sizes past the previously-published 10K
numbers, driving the **real binary over MCP stdio** (one persistent process per
size — the numbers reflect per-op cost, not process spawn). Fully offline,
deterministic (seeded corpus), no network, no API key.

## Measured baselines (committed `report.json`)

Post-optimization run — v2.19.1 main with #476 (signature-driven dedup scan),
#507 (covering dense index), and #511 (hybrid sparse-arm hydration +
concurrent arms) merged; AMD64 16-core, Windows 11 (full hardware + commit in
`report.json`, sha256-signed):

| Metric | 10K | 100K |
| --- | --- | --- |
| Write throughput, sustained | 479/s | 40/s |
| Write throughput, first→last 10% | 1370/s → 288/s | 469/s → 18/s |
| fts5 recall p50 / p99 | 3.1 / 9.6 ms | 15.7 / 23.2 ms |
| dense recall p50 / p99 | 12.0 / 14.3 ms | 25.4 / 32.8 ms |
| hybrid recall p50 / p99 | 19.0 / 24.6 ms | 79.7 / 92.9 ms |
| `as_of` point lookup p50 | 0.17 ms | 0.12 ms |
| temporal recall p50 | 3.7 ms | 12.5 ms |
| Cold start (spawn + init + first query) | 56.6 ms | 58.2 ms |
| DB on disk | ~66 MB | ~890 MB |

Headlines:

- **Bi-temporal stays flat at scale.** `as_of` point lookups sit at ~0.1 ms p50
  at both 10K and 100K, and transaction-time reconstruction recall is under
  20 ms p99 at 100K. The differentiator holds.
- **The write path is fixed.** The first baseline measured 141/s → 7/s from
  10K → 100K (a ~4h bulk load, O(N·body_size) dedup scan per write); after
  #476 it is ~480/s → ~40/s and a 100K load takes ~40 minutes. History and
  methodology in PERF.md.
- **Recall is flat-ish at scale in every mode.** Dense: 390 ms p50 @100K
  before #507's covering index, ~25 ms after. Hybrid (the default mode):
  263 ms p50 @100K before #511, 79.7 ms after — the "fusion-machinery
  overhead" was the sparse arm hydrating every FTS match; it now ranks
  inside the FTS index and the two arms run concurrently. Attribution
  tables in PERF.md.

## Running

```bash
cargo build --release
python benchmark/scale/run.py                          # 10K + 100K (~45 min)
python benchmark/scale/run.py --sizes 10000            # quick (~1 min)
python benchmark/scale/run.py --sizes 1000000          # 1M — manual/nightly (~7h extrapolated)
python benchmark/scale/run.py --skip-embed             # no dense index build
```

Raw runs write to OS temp by default; the committed `report.json` is a curated
artifact — regenerate deliberately with `--out benchmark/scale/report.json`.

## Latency budgets (enforced by `gate.py`)

Per-size budgets with ~3× headroom over the measured baselines (more on
sub-millisecond metrics where absolute jitter dominates), so CI-runner variance
doesn't flake and a failure means a genuine regression. Override any budget via
`SCALE_BUDGET_<NAME>`; select the size via `SCALE_GATE_SIZE`.

| Budget | 10K | 100K | Env override |
| --- | --- | --- | --- |
| Write throughput (sustained) | ≥ 150/s | ≥ 15/s | `SCALE_BUDGET_WRITE_DOCS_PER_SEC` |
| Write throughput (last 10%) | ≥ 100/s | ≥ 7/s | `SCALE_BUDGET_WRITE_LAST10_DOCS_PER_SEC` |
| fts5 recall p99 | ≤ 30 ms | ≤ 100 ms | `SCALE_BUDGET_FTS5_P99_MS` |
| dense recall p99 | ≤ 60 ms | ≤ 150 ms | `SCALE_BUDGET_DENSE_P99_MS` |
| hybrid recall p99 | ≤ 100 ms | ≤ 250 ms | `SCALE_BUDGET_HYBRID_P99_MS` |
| `as_of` p99 | ≤ 5 ms | ≤ 5 ms | `SCALE_BUDGET_AS_OF_P99_MS` |
| temporal recall p99 | ≤ 15 ms | ≤ 50 ms | `SCALE_BUDGET_TEMPORAL_RECALL_P99_MS` |
| Cold start (median) | ≤ 500 ms | ≤ 500 ms | `SCALE_BUDGET_COLD_START_MS` |

Hybrid's 100K budget was tightened by #511 (1000 → 250 ms) and now sits
deliberately BELOW the pre-#511 measured p99 (282–337 ms), so the sparse-arm
hydration disease cannot silently return.

CI (`scale-gate.yml`): the **10K gate runs on every push to main** (about a
minute of load); the **100K run is weekly** (~45 min post-#476) and on
`workflow_dispatch`.

## 1M note

1M is manual/nightly: at the post-#476 write rate a 1M load extrapolates to
roughly 7 hours — feasible on a dedicated box, not in a CI job. If anything
degrades non-linearly on your hardware (recall, as_of, cold start), file a
follow-up with the profile.
