# Scale benchmark — 10K / 100K / 1M entities (#474)

Measures Perseus Vault at corpus sizes past the previously-published 10K
numbers, driving the **real binary over MCP stdio** (one persistent process per
size — the numbers reflect per-op cost, not process spawn). Fully offline,
deterministic (seeded corpus), no network, no API key.

## Measured baselines (committed `report.json`)

First clean run — perseus-vault 2.19.0, AMD64 16-core, Windows 11 (full
hardware + commit in `report.json`, sha256-signed):

| Metric | 10K | 100K |
| --- | --- | --- |
| Write throughput, sustained | 141/s | **7/s** |
| Write throughput, first→last 10% | 1107/s → 68/s | 117/s → **3/s** |
| fts5 recall p50 / p99 | 2.3 / 6.0 ms | 16.5 / 181.7 ms |
| dense recall p50 / p99 | 13.3 / 16.6 ms | 390.5 / 446.9 ms |
| hybrid recall p50 / p99 | 19.6 / 23.0 ms | 597.4 / 639.1 ms |
| `as_of` point lookup p99 | 0.34 ms | **0.32 ms** |
| temporal recall (`as_of_unix_ms`) p50 / p99 | 2.6 / 3.4 ms | 11.7 / 13.5 ms |
| Cold start (spawn + init + first query) | 24.8 ms | 70.2 ms |
| DB on disk | 87 MB | 886 MB |

Two headlines:

- **Bi-temporal stays flat at scale.** `as_of` point lookups are ~0.3 ms p99 at
  both 10K and 100K, and transaction-time reconstruction recall is 13.5 ms p99
  at 100K. The differentiator holds.
- **The write path degrades super-linearly** — sustained throughput drops
  141/s → 7/s from 10K → 100K, and within a single 100K load the last 10% runs
  at 3/s (a 100K bulk load takes ~4h). This is the documented input to the
  #476 write-path optimization; the budgets below lock in "no worse" until it
  lands, then get tightened.

## Running

```bash
cargo build --release
python benchmark/scale/run.py                          # 10K + 100K (~4.5h, see above)
python benchmark/scale/run.py --sizes 10000            # quick (~3 min)
python benchmark/scale/run.py --sizes 1000000          # 1M — manual only, ~days until #476
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
| Write throughput (sustained) | ≥ 50/s | ≥ 3/s | `SCALE_BUDGET_WRITE_DOCS_PER_SEC` |
| Write throughput (last 10%) | ≥ 20/s | ≥ 1/s | `SCALE_BUDGET_WRITE_LAST10_DOCS_PER_SEC` |
| fts5 recall p99 | ≤ 30 ms | ≤ 500 ms | `SCALE_BUDGET_FTS5_P99_MS` |
| dense recall p99 | ≤ 60 ms | ≤ 1200 ms | `SCALE_BUDGET_DENSE_P99_MS` |
| hybrid recall p99 | ≤ 80 ms | ≤ 1800 ms | `SCALE_BUDGET_HYBRID_P99_MS` |
| `as_of` p99 | ≤ 5 ms | ≤ 5 ms | `SCALE_BUDGET_AS_OF_P99_MS` |
| temporal recall p99 | ≤ 15 ms | ≤ 50 ms | `SCALE_BUDGET_TEMPORAL_RECALL_P99_MS` |
| Cold start (median) | ≤ 500 ms | ≤ 1000 ms | `SCALE_BUDGET_COLD_START_MS` |

CI (`scale-gate.yml`): the **10K gate runs on every push to main** (minutes);
the **100K run is weekly** (fits the 6h job limit at today's ~4h load) and on
`workflow_dispatch`. When #476 lands, move 100K into the push path and tighten
the write floors.

## 1M note

1M is deliberately manual until #476: extrapolating today's write curve puts a
1M load at multiple days. If anything ELSE degrades non-linearly on your
hardware (recall, as_of, cold start), file a follow-up with the profile.
