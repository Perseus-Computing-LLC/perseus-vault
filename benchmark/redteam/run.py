#!/usr/bin/env python3
"""Red-team harness runner (skeleton phase).

Validation-only until the attack drivers and defense-eval layer land:
    python3 benchmark/redteam/run.py --validate
    python3 benchmark/redteam/run.py --manifest

Exit 0 when all validators pass; prints a JSON summary.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402


def validate_all() -> dict:
    results = {
        "mafia_probe_set": harness.validate_probe_set(
            harness.load_json(harness.MAFIA_PROBE_SET)),
        "salami_scenarios": harness.validate_salami_scenarios(
            harness.load_json(harness.SALAMI_SCENARIOS)),
    }
    # cloak lint across the worked probe set
    cloak_hits = []
    for probe in harness.load_json(harness.MAFIA_PROBE_SET)["probes"]:
        cloak_hits.extend(harness.cloak_lint(probe.get("cloak_payload") or ""))
    results["cloak_lint"] = cloak_hits
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="run dataset validators")
    parser.add_argument("--manifest", action="store_true",
                        help="print harness + dataset content hashes for a run manifest")
    args = parser.parse_args()

    if args.manifest:
        print(json.dumps({
            "harness_sha256": harness.manifest_sha256(),
            "dataset_sha256": harness.dataset_sha256(),
        }, indent=2))
        return 0

    if args.validate:
        results = validate_all()
        failed = [k for k, v in results.items() if v]
        summary = {
            "benchmark": "perseus-vault-redteam-skeleton",
            "status": "failed" if failed else "passed",
            "checks": results,
            "passed": not failed,
        }
        print(json.dumps(summary, indent=2))
        return 1 if failed else 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
