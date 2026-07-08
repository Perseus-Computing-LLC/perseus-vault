#!/usr/bin/env python3
"""CI latency-budget gate for the scale benchmark (#474).

Runs the scale harness at the gated corpus size (default 10K on the fast CI
path; the weekly workflow dispatches 100K — a 100K bulk load currently takes
~4h, see the #476 write-path issue) and asserts the documented budgets from
README.md. Budgets are per-size and conservative — roughly 3× headroom over
the first committed run (benchmark/scale/report.json names that hardware) —
so a pass means "no regression" and a failure means something genuinely
degraded at scale.

Every budget is env-overridable (SCALE_BUDGET_*) so the workflow file is the
single place budgets get tuned, mirroring perf-gate.yml.

Exit 0 on pass, 1 on failure. Usage: python benchmark/scale/gate.py [--bin PATH]
                                     [--report existing-report.json]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Measured baselines live in report.json; budgets carry ~3x headroom (more on
# sub-millisecond metrics, where absolute jitter dominates). The 100K write
# floors look grim because the O(N)-ish per-write dedup/embed cost is real —
# documented reality, locked in so it can only improve (#476 tracks the fix;
# tighten these when it lands).
DEFAULT_BUDGETS = {
    10_000: {
        "WRITE_DOCS_PER_SEC": 50,          # measured 141
        "WRITE_LAST10_DOCS_PER_SEC": 20,   # measured 68
        "FTS5_P99_MS": 30,                 # measured 6.0
        "DENSE_P99_MS": 60,                # measured 16.6
        "HYBRID_P99_MS": 80,               # measured 23.0
        "AS_OF_P99_MS": 5,                 # measured 0.34
        "TEMPORAL_RECALL_P99_MS": 15,      # measured 3.4
        "COLD_START_MS": 500,              # measured 24.8
    },
    100_000: {
        "WRITE_DOCS_PER_SEC": 3,           # measured 7 (see #476)
        "WRITE_LAST10_DOCS_PER_SEC": 1,    # measured 3 (see #476)
        "FTS5_P99_MS": 500,                # measured 181.7 (p95 22.7)
        "DENSE_P99_MS": 1200,              # measured 446.9
        "HYBRID_P99_MS": 1800,             # measured 639.1
        "AS_OF_P99_MS": 5,                 # measured 0.32 — flat 10K→100K
        "TEMPORAL_RECALL_P99_MS": 50,      # measured 13.5
        "COLD_START_MS": 1000,             # measured 70.2 @ 885MB
    },
}


def budget(size_defaults, name):
    return float(os.environ.get(f"SCALE_BUDGET_{name}", size_defaults[name]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=None)
    ap.add_argument("--report", default=None,
                    help="Gate an existing report instead of running the harness")
    args = ap.parse_args()

    size = int(os.environ.get("SCALE_GATE_SIZE", "10000"))
    if size not in DEFAULT_BUDGETS:
        sys.exit(f"no default budgets for size {size} (have: {sorted(DEFAULT_BUDGETS)}); "
                 "add a row or override every SCALE_BUDGET_* env var")
    b = DEFAULT_BUDGETS[size]

    if args.report:
        report_path = Path(args.report)
    else:
        report_path = Path(tempfile.gettempdir()) / "vault-scale-gate-report.json"
        cmd = [sys.executable, str(HERE / "run.py"), "--sizes", str(size),
               "--out", str(report_path)]
        if args.bin:
            cmd += ["--bin", args.bin]
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            sys.exit(f"scale harness failed (exit {rc})")

    report = json.loads(report_path.read_text(encoding="utf-8"))
    run = report["runs"].get(str(size))
    if not run:
        sys.exit(f"report has no run at size {size} (has: {list(report['runs'])})")

    checks = [
        ("write docs/s (sustained)", run["write"]["docs_per_sec"],
         ">=", budget(b, "WRITE_DOCS_PER_SEC")),
        ("write last-10% docs/s (degradation)", run["write"]["last_10pct_docs_per_sec"],
         ">=", budget(b, "WRITE_LAST10_DOCS_PER_SEC")),
        ("fts5 recall p99 ms", run["recall"]["fts5"]["p99_ms"],
         "<=", budget(b, "FTS5_P99_MS")),
        ("as_of point-lookup p99 ms", run["as_of"]["p99_ms"],
         "<=", budget(b, "AS_OF_P99_MS")),
        ("temporal recall p99 ms", run["temporal_recall"]["p99_ms"],
         "<=", budget(b, "TEMPORAL_RECALL_P99_MS")),
        ("cold start median ms", run["cold_start"]["first_query_ms_median"],
         "<=", budget(b, "COLD_START_MS")),
    ]
    if "hybrid" in run.get("recall", {}):
        checks += [
            ("hybrid recall p99 ms", run["recall"]["hybrid"]["p99_ms"],
             "<=", budget(b, "HYBRID_P99_MS")),
            ("dense recall p99 ms", run["recall"]["dense"]["p99_ms"],
             "<=", budget(b, "DENSE_P99_MS")),
        ]

    failures = []
    print(f"SCALE-GATE | size={size}")
    for label, actual, op, bound in checks:
        ok = (actual >= bound) if op == ">=" else (actual <= bound)
        print(f"SCALE-GATE | {label}: {actual} (budget {op} {bound}) "
              f"{'ok' if ok else 'FAIL'}")
        if not ok:
            failures.append(label)

    if failures:
        sys.exit(f"scale gate FAILED: {', '.join(failures)}")
    print("SCALE-GATE | all budgets met")


if __name__ == "__main__":
    main()
