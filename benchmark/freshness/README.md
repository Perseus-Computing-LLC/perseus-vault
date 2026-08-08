# Freshness and failure benchmark

The suite measures write-to-FTS readability lag, explicit `RecallOutcome`
classification, a bounded deadline probe, and recall after process restart.
It reports p50/p95/p99 latency and outcome-class counts without including raw
markers in the report.

```bash
python3 benchmark/freshness/run.py --bin target/release/perseus-vault \
  --out /tmp/perseus-vault-freshness-report.json
```

Current checked run:

```text
19 / 19 checks passed
p50 write-to-FTS readability: approximately 0.805 ms
```

The provider-failure lane remains intentionally separate: it requires an
injectable embedding-provider failure or a controlled backend fixture. The
local run above measured the healthy local path and did not claim provider
failure coverage.
