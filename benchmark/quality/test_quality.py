#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from run import evaluate_report, load_manifest, run_benchmark
from scorecard import build_scorecard


class QualityHarnessTests(unittest.TestCase):
    def test_manifest_has_required_quality_categories(self):
        manifest = load_manifest(Path(__file__).with_name("manifest.json"))
        self.assertEqual(
            {case["category"] for case in manifest["cases"]},
            {"long_horizon", "contradiction_supersession", "shared_memory", "adversarial"},
        )

    def test_evaluate_report_requires_every_category_and_all_checks(self):
        report = {
            "benchmark": "perseus-vault-memory-quality",
            "cases": [
                {"category": category, "checks": {"passed": 1, "total": 1}}
                for category in (
                    "long_horizon",
                    "contradiction_supersession",
                    "shared_memory",
                    "adversarial",
                )
            ],
        }
        result = evaluate_report(report)
        self.assertTrue(result["passed"])
        self.assertEqual(result["checks_passed"], 4)
        self.assertEqual(result["checks_total"], 4)

    def test_binary_backed_report_runs_real_quality_scenarios(self):
        out = Path(tempfile.mkdtemp()) / "report.json"
        report = run_benchmark(Path(__file__).with_name("manifest.json"), None, out)
        self.assertEqual(report["dataset"], "perseus-vault-memory-quality-v1")
        self.assertEqual(report["checks_total"], 8)
        self.assertEqual({case["checks"]["total"] for case in report["cases"]}, {2})
        self.assertTrue(all(case["evidence"] for case in report["cases"]))
        self.assertTrue(report["passed"])

    def test_evaluate_report_rejects_missing_or_failed_checks(self):
        report = {
            "benchmark": "perseus-vault-memory-quality",
            "cases": [
                {"category": "long_horizon", "checks": {"passed": 1, "total": 1}},
                {"category": "contradiction_supersession", "checks": {"passed": 0, "total": 1}},
            ],
        }
        result = evaluate_report(report)
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_categories"], ["adversarial", "shared_memory"])

    def test_scorecard_marks_a_fully_passing_report_release_ready(self):
        report = {
            "benchmark": "perseus-vault-memory-quality",
            "dataset": "quality-v1",
            "passed": True,
            "checks_passed": 8,
            "checks_total": 8,
            "accuracy": 1.0,
            "missing_categories": [],
            "cases": [
                {"category": category, "checks": {"passed": 2, "total": 2}}
                for category in (
                    "long_horizon",
                    "contradiction_supersession",
                    "shared_memory",
                    "adversarial",
                )
            ],
        }
        scorecard = build_scorecard(report)
        self.assertEqual(scorecard["verdict"], "release_ready")
        self.assertTrue(scorecard["blocking"])
        self.assertEqual(scorecard["thresholds"]["minimum_accuracy"], 1.0)

    def test_scorecard_blocks_a_regression_and_names_failed_categories(self):
        report = {
            "benchmark": "perseus-vault-memory-quality",
            "dataset": "quality-v1",
            "passed": False,
            "checks_passed": 7,
            "checks_total": 8,
            "accuracy": 0.875,
            "missing_categories": [],
            "cases": [
                {"category": "long_horizon", "checks": {"passed": 2, "total": 2}},
                {"category": "contradiction_supersession", "checks": {"passed": 1, "total": 2}},
                {"category": "shared_memory", "checks": {"passed": 2, "total": 2}},
                {"category": "adversarial", "checks": {"passed": 2, "total": 2}},
            ],
        }
        scorecard = build_scorecard(report)
        self.assertEqual(scorecard["verdict"], "blocked")
        self.assertEqual(scorecard["failed_categories"], ["contradiction_supersession"])
        self.assertEqual(scorecard["override_policy"]["required_approver"], "maintainer")


if __name__ == "__main__":
    unittest.main()
