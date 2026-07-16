#!/usr/bin/env python3
"""CI gate for the BEAM at-scale bi-temporal benchmark (#685).

Fails (non-zero exit) if any run tier regressed:
  * gauntlet correctness must stay 100% (bi-temporal correctness must NOT
    degrade with corpus size — the entire point of BEAM),
  * results must be deterministic across the two independent runs,
  * per-axis point-lookup latency must stay under budget.

Usage:
    python benchmark/beam/run.py --tiers 128K --out /tmp/beam.json
    python benchmark/beam/gate.py /tmp/beam.json
"""
import json
import sys
from pathlib import Path

# Point-lookup latency budgets (ms, p95) by tier. Bi-temporal point lookups are
# indexed by (category,key), so they should stay near-flat as N grows; these are
# generous ceilings, not targets. Tune against measured hardware in report.json.
P95_BUDGET_MS = {"128K": 60.0, "500K": 90.0, "1M": 150.0, "10M": 400.0}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent / "report.json")
    report = json.loads(Path(path).read_text(encoding="utf-8"))
    failures = []
    for t in report.get("tiers", []):
        name = t["tier"]
        if t.get("gauntlet_accuracy") != 1.0:
            failures.append(f"{name}: correctness {t.get('gauntlet_checks')} != 100%")
        if not t.get("deterministic"):
            failures.append(f"{name}: non-deterministic (signatures differ across runs)")
        budget = P95_BUDGET_MS.get(name)
        if budget is not None:
            for axis, m in t.get("latency", {}).items():
                if m.get("p95_ms", 0) > budget:
                    failures.append(f"{name}: {axis} p95 {m['p95_ms']}ms > {budget}ms budget")

    if failures:
        print("BEAM gate: FAIL")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"BEAM gate: PASS ({len(report.get('tiers', []))} tier(s))")
    sys.exit(0)


if __name__ == "__main__":
    main()
