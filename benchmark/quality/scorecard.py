#!/usr/bin/env python3
"""Build a release decision scorecard from a memory-quality report (#779)."""
import argparse
import json
import sys
from pathlib import Path

MINIMUM_ACCURACY = 1.0
REQUIRED_APPROVER = "maintainer"


def build_scorecard(report):
    failed_categories = sorted(
        case.get("category")
        for case in report.get("cases", [])
        if int(case.get("checks", {}).get("passed", 0))
        < int(case.get("checks", {}).get("total", 0))
    )
    missing = sorted(report.get("missing_categories", []))
    accuracy = float(report.get("accuracy", 0.0))
    release_ready = (
        report.get("passed") is True
        and accuracy >= MINIMUM_ACCURACY
        and not failed_categories
        and not missing
    )
    return {
        "scorecard_version": "perseus-vault-memory-quality-scorecard/v1",
        "benchmark": report.get("benchmark"),
        "dataset": report.get("dataset"),
        "verdict": "release_ready" if release_ready else "blocked",
        "blocking": True,
        "checks_passed": int(report.get("checks_passed", 0)),
        "checks_total": int(report.get("checks_total", 0)),
        "accuracy": accuracy,
        "failed_categories": failed_categories,
        "missing_categories": missing,
        "thresholds": {
            "minimum_accuracy": MINIMUM_ACCURACY,
            "all_required_categories_present": True,
            "all_category_checks_pass": True,
        },
        "override_policy": {
            "allowed": True,
            "required_approver": REQUIRED_APPROVER,
            "requirements": [
                "document the failing checks and user impact",
                "link a remediation issue",
                "record the override in release notes",
            ],
        },
        "source_report_signature": report.get("signature_sha256"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    scorecard = build_scorecard(report)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(scorecard, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(scorecard, indent=2))
    return 0 if scorecard["verdict"] == "release_ready" else 1


if __name__ == "__main__":
    sys.exit(main())
