#!/usr/bin/env python3
"""CI latency-budget gate for the scale benchmark (#474).

Runs the scale harness at the gated corpus size (default 10K on the fast CI
path; the weekly workflow dispatches 100K — a 100K bulk load currently takes
~4h, see the #476 write-path issue) and asserts the documented budgets from
README.md. Budgets are per-size and conservative — roughly 3× headroom over
the measured baseline on the runner the gate runs on (2-vCPU ubuntu-latest;
recalibrated 2026-08-08 in #898/#900 — see the DEFAULT_BUDGETS comment) — so
a pass means "no regression" and a failure means something genuinely
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
# sub-millisecond metrics, where absolute jitter dominates).
#
# 2026-08-08 (#898 / #900) — budgets recalibrated to the runner the gate
# ACTUALLY runs on. The previous budgets came from the committed report.json
# (16-core Windows box); the gate runs on GitHub's shared 2-vCPU
# ubuntu-latest, which measures ~25-35x slower for fts5/temporal and made the
# gate permanently red. New baselines:
#   - 10K: measured on the gate runner itself via workflow_dispatch
#     (run 31284616793, head 2afee8a, post-#899 batched suppression filter):
#     fts5 p99 322.7ms, temporal p99 296.0ms, hybrid p99 204.0ms (was
#     334-354 pre-#899), dense p99 38.9ms, as_of 0.31ms, cold start 377.9ms,
#     write 1260/1160 docs/s. Budgets = measured x3.
#   - 100K: 8-vCPU reference reports (report-v2.21.0-linux-8vcpu-100k.json)
#     x3 as a START — first weekly 100K run may need a follow-up tweak:
#     the 10K 2vCPU/8vCPU ratios suggest temporal (~35x) and cold start
#     (~16x) could land well above these on 2-vCPU.
DEFAULT_BUDGETS = {
    10_000: {
        "WRITE_DOCS_PER_SEC": 420,         # measured 1260 (2-vCPU, 2026-08-08)
        "WRITE_LAST10_DOCS_PER_SEC": 386,  # measured 1160
        "FTS5_P99_MS": 968,                # measured 322.7
        "DENSE_P99_MS": 117,               # measured 38.9
        "HYBRID_P99_MS": 612,              # measured 204.0 (was 334-354 pre-#899)
        "AS_OF_P99_MS": 5,                 # measured 0.31 — sub-ms, extra headroom
        "TEMPORAL_RECALL_P99_MS": 888,     # measured 296.0
        "COLD_START_MS": 1134,             # measured 377.9
    },
    100_000: {
        "WRITE_DOCS_PER_SEC": 8,           # 8-vCPU ref 26 /3 (first weekly may need tweak)
        "WRITE_LAST10_DOCS_PER_SEC": 4,    # 8-vCPU ref 14 /3
        "FTS5_P99_MS": 375,                # 8-vCPU ref 125.0 x3
        "DENSE_P99_MS": 499,               # 8-vCPU ref 166.3 x3
        "HYBRID_P99_MS": 1173,             # 8-vCPU ref 390.9 x3
        "AS_OF_P99_MS": 132,               # 8-vCPU ref 44.0 x3
        "TEMPORAL_RECALL_P99_MS": 241,     # 8-vCPU ref 80.2 x3 — 2-vCPU est ~2.8s, likely tweak
        "COLD_START_MS": 686,              # 8-vCPU ref 228.7 x3 — 2-vCPU est ~3.6s, likely tweak
    },
    # 1M rung (#589). Keyword/hybrid latency budgets carry the corpus-scaling
    # MEASURED on the #589 Lambda A10 run (benchmark/lambda/results/, 2026-07-12):
    # broad-term keyword search is inherently ~5-6x slower at 1M than 100K — the
    # UNPATCHED fts5 path scales the same way (broad p95 67ms@100K -> 396ms@1M),
    # so this is FTS5 posting-list growth, not a regression. The two-phase sparse
    # arm's ADDED O(matches) superlinearity (the #511 residual: broad p50
    # 181ms@100K -> 2699ms@1M, ~15x) is what PERSEUS_VAULT_BM25_SCAN_CAP bounds: at
    # cap=2048 the 1M broad-term sparse p50 drops to ~450ms (~5x, i.e. down to the
    # inherent FTS floor), exact whenever the match set <= cap. The cap is OFF by
    # default (opt-in dial, #617); these budgets therefore reflect the shipped
    # cap=0 behavior with generous headroom. write/as_of/temporal/cold_start are
    # EXTRAPOLATED from the 100K row (sublinear write degradation, ~10x data for
    # cold start) pending a full scale-harness 1M load (~40h at 46 docs/s — see
    # the #476 write-path note); tighten once that run exists.
    1_000_000: {
        "WRITE_DOCS_PER_SEC": 8,           # extrapolated from 46@100K (sublinear)
        "WRITE_LAST10_DOCS_PER_SEC": 4,    # extrapolated from 21@100K
        "FTS5_P99_MS": 900,                # measured broad fts5 p95 ~396ms @1M + headroom
        "DENSE_P99_MS": 500,               # extrapolated: sig-prefilter ~linear in embedded rows
        "HYBRID_P99_MS": 1500,             # cap=0 broad-tail dominated; opt-in cap bounds sparse arm
        "AS_OF_P99_MS": 5,                 # point lookup — flat across scale
        "TEMPORAL_RECALL_P99_MS": 100,     # extrapolated from 13.3@100K
        "COLD_START_MS": 2000,             # extrapolated: ~10x data (~9GB)
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
