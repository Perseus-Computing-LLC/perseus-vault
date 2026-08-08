#!/usr/bin/env python3
import json
import queue
import tempfile
import unittest
from pathlib import Path

from benchmark.package.common.artifacts import result_signature
from run import (
    build_metric_rates,
    case_result,
    compute_metrics,
    evaluate_report,
    find_binary,
    load_manifest,
    projection_budget_bounded,
    report_signature_payload,
    run_benchmark,
    sanitize_evidence,
    VaultClient,
)
from scorecard import build_scorecard


class QualityHarnessTests(unittest.TestCase):
    def test_manifest_has_required_quality_categories(self):
        manifest = load_manifest(Path(__file__).with_name("manifest.json"))
        categories = {case["category"] for case in manifest["cases"]}
        self.assertTrue(
            {"long_horizon", "contradiction_supersession", "shared_memory", "adversarial"}
            .issubset(categories)
        )

    def test_manifest_has_bounded_v0_metric_coverage(self):
        manifest = load_manifest(Path(__file__).with_name("manifest.json"))
        self.assertGreaterEqual(len(manifest["cases"]), 20)
        self.assertLessEqual(len(manifest["cases"]), 30)
        self.assertTrue(
            {
                "validity",
                "scope_invalid_recall",
                "provenance",
                "replay_fidelity",
                "mutation_supersession",
                "compaction_projection",
                "action_grounding",
            }.issubset({case["metric"] for case in manifest["cases"]})
        )
        self.assertEqual(
            len({case["id"] for case in manifest["cases"]}),
            len(manifest["cases"]),
        )

    def test_load_manifest_normalizes_legacy_v1_four_case_shape(self):
        legacy = {
            "name": "perseus-vault-memory-quality-v1",
            "version": 1,
            "cases": [
                {"id": category, "category": category, "checks": ["ok"]}
                for category in (
                    "long_horizon",
                    "contradiction_supersession",
                    "shared_memory",
                    "adversarial",
                )
            ],
        }
        path = Path(tempfile.mkdtemp()) / "legacy.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        loaded = load_manifest(path)
        self.assertEqual(len(loaded["cases"]), 4)
        self.assertEqual(
            set(loaded["required_categories"]),
            {case["category"] for case in legacy["cases"]},
        )
        self.assertTrue(all(case["scenario"] == case["category"] for case in loaded["cases"]))

    def test_public_evidence_is_hash_only_and_drops_raw_inputs(self):
        evidence = sanitize_evidence(
            {
                "id": "mem-random",
                "body_json": "private body sentinel",
                "query": "private query sentinel",
                "arguments": {"token": "private argument sentinel"},
                "nested": {"prompt": "private prompt sentinel", "key": "safe-key"},
            }
        )
        encoded = json.dumps(evidence, sort_keys=True)
        for forbidden in (
            "body_json",
            "query",
            "arguments",
            "prompt",
            "private body sentinel",
            "private query sentinel",
            "private argument sentinel",
            "private prompt sentinel",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn("id_sha256", encoded)
        self.assertNotIn("mem-random", encoded)
        self.assertNotIn("nested", encoded)

    def test_case_result_rejects_evidence_with_no_public_fields(self):
        with self.assertRaises(ValueError):
            case_result(
                {"id": "case", "category": "quality", "checks": ["ok"]},
                {"ok": True},
                {"query": "private query sentinel", "body": "private body sentinel"},
            )

    def test_case_result_rejects_mixed_forbidden_evidence(self):
        with self.assertRaises(ValueError):
            case_result(
                {"id": "case", "category": "quality", "checks": ["ok"]},
                {"ok": True},
                {"count": 1, "query": "private-query-sentinel"},
            )

    def test_case_result_hashes_failure_class_evidence(self):
        result = case_result(
            {"id": "case", "category": "quality", "checks": ["ok"]},
            {"ok": False},
            {"failure_class": "MCPError"},
            status="failed",
            failure_class="MCPError",
        )
        self.assertRegex(result["evidence"]["failure_class"], r"^[0-9a-f]{64}$")

    def test_public_evidence_drops_credentials_timestamps_and_unknown_keys(self):
        evidence = sanitize_evidence(
            {
                "credentials": {"password": "pw", "api_key": "key"},
                "access_token": "token",
                "tokens": ["token"],
                "authorization": "Bearer secret",
                "timestamp_ms": 123,
                "recorded_at": 456,
                "unknown_numeric": 7,
                "count": 2,
            }
        )
        self.assertEqual(evidence, {"count": 2})

    def test_projection_budget_uses_injected_chars_not_response_envelope(self):
        self.assertTrue(
            projection_budget_bounded(
                {"budget_chars": 240, "injected_chars": 240, "total_chars": 244},
                240,
            )
        )
        self.assertFalse(
            projection_budget_bounded(
                {"budget_chars": 240, "injected_chars": 241, "total_chars": 245},
                240,
            )
        )
        self.assertFalse(projection_budget_bounded({}, 240))
        self.assertFalse(projection_budget_bounded({"budget_chars": 240.5, "injected_chars": 240.9}, 240))
        self.assertFalse(projection_budget_bounded({"budget_chars": "240", "injected_chars": "240"}, 240))

    def test_stale_recall_rate_is_a_zero_good_bad_event_rate(self):
        cases = [
            {
                "id": "mutation-live-recall",
                "status": "passed",
                "assertions": {"superseded_version_not_recalled": True},
            }
        ]
        rates = build_metric_rates(cases, {})
        self.assertEqual(rates["stale_recall_rate"], {"rate": 0.0, "status": "available"})

    def test_mcp_read_has_a_wall_clock_timeout(self):
        client = object.__new__(VaultClient)
        client._responses = queue.Queue()
        client.response_timeout_seconds = 0.01
        with self.assertRaises(TimeoutError):
            client._read()

    def test_case_result_marks_partial_checks_failed(self):
        result = case_result(
            {"id": "partial", "category": "mutation", "metric": "mutation", "checks": ["ok", "missing"]},
            {"ok": True},
            {"complete": True},
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"], {"passed": 1, "total": 2})

    def test_metric_aggregation_keeps_unavailable_capability_explicit(self):
        metrics = compute_metrics(
            [
                {
                    "status": "passed",
                    "checks": {"passed": 1, "total": 1},
                    "metric": {"name": "validity", "numerator": 1, "denominator": 1},
                },
                {
                    "status": "unavailable",
                    "checks": {"passed": 0, "total": 0},
                    "metric": {
                        "name": "compaction_projection",
                        "status": "unavailable",
                        "reason": "tool not advertised",
                    },
                },
            ]
        )
        self.assertEqual(metrics["validity"]["rate"], 1.0)
        self.assertEqual(metrics["compaction_projection"]["status"], "unavailable")
        self.assertEqual(metrics["compaction_projection"]["reason"], "tool not advertised")

    def test_signature_payload_omits_nondeterministic_and_private_evidence(self):
        first = report_signature_payload(
            {
                "dataset": "quality-v0",
                "cases": [
                    {
                        "id": "case-a",
                        "status": "passed",
                        "assertions": {"ok": True},
                        "evidence": {"id_sha256": "a" * 64, "timestamp": 1},
                    }
                ],
                "metrics": {"validity": {"rate": 1.0}},
            }
        )
        second = report_signature_payload(
            {
                "dataset": "quality-v0",
                "cases": [
                    {
                        "id": "case-a",
                        "status": "passed",
                        "assertions": {"ok": True},
                        "evidence": {"id_sha256": "b" * 64, "timestamp": 999},
                    }
                ],
                "metrics": {"validity": {"rate": 1.0}},
            }
        )
        self.assertEqual(first, second)
        self.assertNotIn("timestamp", json.dumps(first, sort_keys=True))

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
        try:
            find_binary(None)
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        out = Path(tempfile.mkdtemp()) / "report.json"
        report = run_benchmark(Path(__file__).with_name("manifest.json"), None, out)
        self.assertEqual(report["dataset"], "perseus-vault-memory-quality-v1")
        self.assertEqual(report["checks_total"], 41)
        self.assertEqual(len(report["cases"]), 30)
        self.assertTrue(all(case["evidence"] for case in report["cases"]))
        self.assertTrue(report["passed"])

    def test_scorecard_rejects_resigned_manifest_case_substitution(self):
        try:
            find_binary(None)
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        out = Path(tempfile.mkdtemp()) / "report.json"
        report = run_benchmark(Path(__file__).with_name("manifest.json"), None, out)
        forged = json.loads(json.dumps(report))
        forged["cases"][0]["id"] = "forged-case-id"
        forged["result_signature_sha256"] = result_signature(forged)
        scorecard = build_scorecard(forged)
        self.assertEqual(scorecard["verdict"], "blocked")
        self.assertEqual(scorecard["reason"], "incomplete_case_contract")

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
            "metric_rates": {
                name: {"rate": 0.0 if name == "stale_recall_rate" else 1.0, "status": "available"}
                for name in (
                    "validity_rate",
                    "stale_recall_rate",
                    "scope_invalid_recall_rate",
                    "provenance_completeness",
                    "replay_fidelity",
                    "mutation_supersession_rate",
                    "compaction_projection_rate",
                    "action_grounding_rate",
                )
            },
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
        self.assertEqual(scorecard["verdict"], "blocked")
        self.assertTrue(scorecard["blocking"])
        self.assertEqual(scorecard["reason"], "incomplete_publication_envelope")

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
        self.assertEqual(scorecard["reason"], "incomplete_publication_envelope")
    def test_scorecard_blocks_explicit_unavailable_capability(self):
        report = {
            "benchmark": "perseus-vault-memory-quality",
            "dataset": "quality-v0",
            "passed": False,
            "checks_passed": 1,
            "checks_total": 1,
            "accuracy": 1.0,
            "missing_categories": [],
            "unavailable_categories": ["compaction_projection"],
            "unavailable_cases": ["compaction-archive"],
            "cases": [
                {"category": "validity", "status": "passed", "checks": {"passed": 1, "total": 1}},
                {"category": "compaction_projection", "status": "unavailable", "checks": {"passed": 0, "total": 0}},
            ],
            "metrics": {
                "compaction_projection": {
                    "status": "unavailable",
                    "reason": "tool not advertised",
                }
            },
        }
        scorecard = build_scorecard(report)
        self.assertEqual(scorecard["verdict"], "blocked")
        self.assertEqual(scorecard["reason"], "incomplete_publication_envelope")
    def test_scorecard_blocks_unavailable_capability_without_category_failure(self):
        report = {
            "benchmark": "perseus-vault-memory-quality",
            "dataset": "quality-v0",
            "passed": True,
            "checks_passed": 1,
            "checks_total": 1,
            "accuracy": 1.0,
            "missing_categories": [],
            "capabilities": {"context": {"status": "unavailable"}},
            "metric_rates": {
                name: {"rate": 1.0, "status": "available"}
                for name in (
                    "validity_rate",
                    "stale_recall_rate",
                    "scope_invalid_recall_rate",
                    "provenance_completeness",
                    "replay_fidelity",
                    "mutation_supersession_rate",
                    "compaction_projection_rate",
                    "action_grounding_rate",
                )
            },
            "cases": [{"category": "validity", "status": "passed", "checks": {"passed": 1, "total": 1}}],
        }
        scorecard = build_scorecard(report)
        self.assertEqual(scorecard["verdict"], "blocked")
        self.assertEqual(scorecard["reason"], "incomplete_publication_envelope")

    def test_scorecard_requires_finite_exact_accuracy_and_consistent_counts(self):
        for accuracy, passed, total in ((1.1, 1, 1), (float("inf"), 1, 1), (1.0, 2, 1)):
            report = {
                "benchmark": "perseus-vault-memory-quality",
                "dataset": "quality-v0",
                "passed": True,
                "checks_passed": passed,
                "checks_total": total,
                "accuracy": accuracy,
                "missing_categories": [],
                "metric_rates": {
                    name: {"rate": 1.0, "status": "available"}
                    for name in (
                        "validity_rate",
                        "stale_recall_rate",
                        "scope_invalid_recall_rate",
                        "provenance_completeness",
                        "replay_fidelity",
                        "mutation_supersession_rate",
                        "compaction_projection_rate",
                        "action_grounding_rate",
                    )
                },
                "cases": [{"category": "validity", "status": "passed", "checks": {"passed": 1, "total": 1}}],
            }
            self.assertEqual(build_scorecard(report)["verdict"], "blocked")
    def test_scorecard_rejects_self_declared_complete_but_structurally_invalid_reports(self):
        metric_rates = {
            name: {"rate": 1.0, "status": "available"}
            for name in (
                "validity_rate",
                "stale_recall_rate",
                "scope_invalid_recall_rate",
                "provenance_completeness",
                "replay_fidelity",
                "mutation_supersession_rate",
                "compaction_projection_rate",
                "action_grounding_rate",
            )
        }
        only_validity = {
            "dataset": "quality-v1",
            "passed": True,
            "checks_passed": 1,
            "checks_total": 1,
            "accuracy": 1.0,
            "missing_categories": [],
            "metric_rates": metric_rates,
            "cases": [{"category": "validity", "status": "passed", "checks": {"passed": 1, "total": 1}}],
        }
        self.assertEqual(build_scorecard(only_validity)["verdict"], "blocked")
        for bad_accuracy in (True, "1.0"):
            malformed_accuracy = {**only_validity, "accuracy": bad_accuracy}
            self.assertEqual(build_scorecard(malformed_accuracy)["verdict"], "blocked")
        huge_accuracy = {**only_validity, "accuracy": 10**1000}
        self.assertEqual(build_scorecard(huge_accuracy)["verdict"], "blocked")
        huge_rate = {**only_validity, "metric_rates": dict(metric_rates)}
        huge_rate["metric_rates"]["validity_rate"] = {"rate": 10**1000, "status": "available"}
        self.assertEqual(build_scorecard(huge_rate)["verdict"], "blocked")
        for bad_rate in ("not-a-number", float("inf")):
            malformed_rate = {**only_validity, "metric_rates": dict(metric_rates)}
            malformed_rate["metric_rates"]["validity_rate"] = {"rate": bad_rate, "status": "available"}
            self.assertEqual(build_scorecard(malformed_rate)["verdict"], "blocked")
        wrong_stale = {**only_validity, "metric_rates": dict(metric_rates)}
        wrong_stale["metric_rates"]["stale_recall_rate"] = {"rate": 1.0, "status": "available"}
        self.assertEqual(build_scorecard(wrong_stale)["verdict"], "blocked")
        v0_categories = [
            "long_horizon",
            "contradiction_supersession",
            "shared_memory",
            "adversarial",
            "validity",
            "scope_validity",
            "provenance",
            "replay",
            "mutation",
            "compaction_projection",
            "action_grounding",
        ]
        v0_cases = [
            {"category": v0_categories[index % len(v0_categories)], "status": "passed", "checks": {"passed": 1, "total": 1}}
            for index in range(19)
        ]
        short_v0 = {
            **only_validity,
            "dataset": "perseus-vault-memory-quality-v0",
            "checks_passed": 19,
            "checks_total": 19,
            "cases": v0_cases,
        }
        self.assertEqual(build_scorecard(short_v0)["verdict"], "blocked")
        self.assertEqual(build_scorecard(short_v0)["reason"], "incomplete_publication_envelope")
        legacy_cases = [
            {"category": category, "status": "passed", "checks": {"passed": 1, "total": 1}}
            for category in ("long_horizon", "contradiction_supersession", "shared_memory", "adversarial")
        ]
        fractional = {
            **only_validity,
            "checks_passed": 4.2,
            "checks_total": 4.0,
            "cases": legacy_cases,
        }
        self.assertEqual(build_scorecard(fractional)["verdict"], "blocked")
        inconsistent = {
            **only_validity,
            "checks_passed": 4,
            "checks_total": 4,
            "cases": [
                {"category": "long_horizon", "status": "passed", "checks": {"passed": 2, "total": 1}},
                *legacy_cases[1:],
            ],
        }
        scorecard = build_scorecard(inconsistent)
        self.assertEqual(scorecard["verdict"], "blocked")
        self.assertEqual(scorecard["reason"], "incomplete_publication_envelope")


if __name__ == "__main__":
    unittest.main()
