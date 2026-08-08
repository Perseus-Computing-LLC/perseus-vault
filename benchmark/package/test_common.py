import json
import math
import tempfile
import unittest
from pathlib import Path

from benchmark.package.common.artifacts import (
    control_profile_digest,
    finalize_report,
    result_signature,
    run_fingerprint,
    sha256_file,
    sha256_text,
    stable_json,
    validate_report,
    write_report,
)


class CommonArtifactTests(unittest.TestCase):
    def test_stable_json_is_order_independent(self):
        self.assertEqual(stable_json({"b": 2, "a": 1}), '{"a":1,"b":2}')
        self.assertEqual(stable_json({"a": 1, "b": 2}), stable_json({"b": 2, "a": 1}))

    def test_control_profile_digest_changes_when_a_control_changes(self):
        first = {"benchmark_id": "smoke", "retrieval": {"k": 5}}
        second = {"benchmark_id": "smoke", "retrieval": {"k": 10}}
        self.assertNotEqual(control_profile_digest(first), control_profile_digest(second))

    def test_result_signature_excludes_runtime_evidence(self):
        first = {
            "schema_version": "perseus-vault-benchmark-report/v1",
            "benchmark_id": "smoke",
            "suite_version": "v1",
            "status": "passed",
            "cases": [{"id": "case", "category": "contract", "status": "passed", "checks": {"ok": True}, "evidence": {"id": "one", "timestamp": 1}}],
            "metrics": {"contract": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
            "not_measured": [],
            "excluded": [],
        }
        second = json.loads(json.dumps(first))
        second["cases"][0]["evidence"] = {"id": "two", "timestamp": 999}
        self.assertEqual(result_signature(first), result_signature(second))

    def test_result_signature_is_case_order_independent(self):
        report = {
            "schema_version": "perseus-vault-benchmark-report/v1",
            "benchmark_id": "smoke",
            "suite_version": "v1",
            "status": "passed",
            "capabilities": {"mcp": {"status": "available"}},
            "cases": [
                {"id": "b", "category": "contract", "status": "passed", "checks": {"ok": True}},
                {"id": "a", "category": "contract", "status": "passed", "checks": {"ok": True}},
            ],
            "metrics": {"contract": {"status": "available", "numerator": 2, "denominator": 2, "rate": 1.0}},
            "not_measured": [],
            "excluded": [],
        }
        reversed_report = {**report, "cases": list(reversed(report["cases"]))}
        self.assertEqual(result_signature(report), result_signature(reversed_report))

    def test_result_signature_binds_capability_status(self):
        report = {
            "schema_version": "perseus-vault-benchmark-report/v1",
            "benchmark_id": "smoke",
            "suite_version": "v1",
            "status": "passed",
            "capabilities": {"mcp": {"status": "available"}},
            "cases": [{"id": "a", "category": "contract", "status": "passed", "checks": {"ok": True}}],
            "metrics": {"contract": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
            "not_measured": [],
            "excluded": [],
        }
        changed = json.loads(json.dumps(report))
        changed["capabilities"]["mcp"]["status"] = "partial"
        self.assertNotEqual(result_signature(report), result_signature(changed))

    def test_stable_json_rejects_non_finite_numbers(self):
        with self.assertRaises(ValueError):
            stable_json({"value": math.nan})
        with self.assertRaises(ValueError):
            stable_json({"value": math.inf})

    def test_validate_report_rejects_missing_binding_and_raw_evidence(self):
        report = self._valid_report()
        del report["run_fingerprint_sha256"]
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["cases"][0]["evidence"] = {"query": "private sentinel"}
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_empty_or_contradictory_pass(self):
        report = self._valid_report()
        report["cases"] = []
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["cases"][0]["status"] = "failed"
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_requires_complete_consistent_metrics(self):
        report = self._valid_report()
        del report["metrics"]["contract"]["rate"]
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["metrics"]["contract"]["numerator"] = 0
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_accepts_a_complete_hash_only_report_and_writes_it(self):
        report = self._valid_report()
        validate_report(report)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            write_report(path, report)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["result_signature_sha256"], report["result_signature_sha256"])

    @staticmethod
    def _valid_report():
        return finalize_report({
            "schema_version": "perseus-vault-benchmark-report/v1",
            "benchmark_id": "smoke",
            "suite_version": "v1",
            "control_profile_sha256": "a" * 64,
            "run_fingerprint_sha256": run_fingerprint(binary_sha256="c" * 64, control_profile_sha256="a" * 64, dataset_sha256="d" * 64, harness_commit="a" * 40, claims_sha256=sha256_text(stable_json({"claim_ids": [], "negative_claim_ids": []}))),
            "binary_sha256": "c" * 64,
            "dataset_sha256": "d" * 64,
            "harness_commit": "a" * 40,
            "claim_ids": [],
            "negative_claim_ids": [],
            "claims_sha256": sha256_text(stable_json({"claim_ids": [], "negative_claim_ids": []})),
            "status": "passed",
            "capabilities": {"mcp": {"status": "available"}},
            "cases": [
                {
                    "id": "case-a",
                    "category": "contract",
                    "status": "passed",
                    "checks": {"ok": True},
                    "evidence": {"count": 1, "complete": True},
                }
            ],
            "metrics": {"contract": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
            "not_measured": [],
            "excluded": [],
            "public_evidence": "hash-only",
            "raw_inputs_captured": False,
            "network_calls": 0,
        })

    def test_validate_report_rejects_unknown_private_fields_and_raw_evidence_flag(self):
        report = self._valid_report()
        report["private_value"] = "token-secret"
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["cases"][0]["query"] = "private query"
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["cases"][0]["evidence"]["raw_inputs_captured"] = True
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_invalid_status_capability_and_metric_reason(self):
        report = self._valid_report()
        report["status"] = "bogus"
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["capabilities"] = {}
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["capabilities"]["mcp"] = {"status": "unavailable"}
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["metrics"]["contract"]["reason"] = "private query token"
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_requires_reason_for_non_available_capability(self):
        report = self._valid_report()
        report["capabilities"]["mcp"] = {"status": "partial"}
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_private_evidence_labels(self):
        for key in ("category", "check", "mode", "reason", "scope", "status"):
            report = self._valid_report()
            report["cases"][0]["evidence"][key] = "private-query-token"
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_report(report)

    def test_validate_report_rejects_private_identifier_tokens(self):
        report = self._valid_report()
        report["cases"][0]["id"] = "private-query-token"
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["cases"][0]["id"] = "scope-invalid-recall-external"
        report["result_signature_sha256"] = result_signature(report)
        validate_report(report)
        report = self._valid_report()
        report["cases"][0]["category"] = "private-query-token"
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        report["benchmark_id"] = "private-query-token"
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_available_metric_rate_without_measurement(self):
        report = self._valid_report()
        report["metric_rates"] = {"suite": {"status": "available"}}
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_non_boolean_case_checks(self):
        report = self._valid_report()
        report["cases"][0]["checks"]["ok"] = 1
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_binds_claims_to_run_fingerprint(self):
        report = self._valid_report()
        report["claim_ids"] = ["claim-a"]
        report["claims_sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_missing_tools_on_available_capability(self):
        report = self._valid_report()
        report["capabilities"]["mcp"] = {
            "status": "available",
            "required_tools": ["mcp_tool"],
            "missing_tools": ["mcp_tool"],
        }
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_nonpassing_status_can_be_explained_by_capability_or_metric_state(self):
        report = self._valid_report()
        report["status"] = "blocked"
        report["capabilities"]["mcp"] = {"status": "unavailable", "reason": "provider_unavailable"}
        report["result_signature_sha256"] = result_signature(report)
        validate_report(report)
        report = self._valid_report()
        report["status"] = "failed"
        report["capabilities"]["mcp"] = {"status": "failed", "reason": "runner_failed"}
        report["result_signature_sha256"] = result_signature(report)
        validate_report(report)
        report = self._valid_report()
        report["status"] = "partial"
        report["capabilities"]["mcp"] = {"status": "partial", "reason": "degraded_backend"}
        report["result_signature_sha256"] = result_signature(report)
        validate_report(report)

    def test_result_signature_binds_failure_class(self):
        report = self._valid_report()
        changed = json.loads(json.dumps(report))
        report["cases"][0]["failure_class"] = "runner_failed"
        changed["cases"][0]["failure_class"] = "provider_failed"
        self.assertNotEqual(result_signature(report), result_signature(changed))

    def test_validate_report_rejects_nested_non_finite_values(self):
        report = self._valid_report()
        report["claim_ids"] = ["claim"]
        report["cases"][0]["evidence"]["rate"] = math.nan
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_rejects_forged_run_fingerprint(self):
        report = self._valid_report()
        report["run_fingerprint_sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_run_fingerprint_binds_binary_profile_dataset_and_commit(self):
        args = dict(binary_sha256="a" * 64, control_profile_sha256="b" * 64, dataset_sha256="c" * 64, harness_commit="a" * 40)
        self.assertNotEqual(run_fingerprint(**args), run_fingerprint(**{**args, "harness_commit": "b" * 40}))
        self.assertNotEqual(run_fingerprint(**args), run_fingerprint(**{**args, "claims_sha256": "d" * 64}))

    def test_validate_report_rejects_unknown_harness_commit(self):
        report = self._valid_report()
        report["harness_commit"] = "unknown"
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_validate_report_requires_explicit_claim_arrays(self):
        report = self._valid_report()
        del report["claim_ids"]
        with self.assertRaises(ValueError):
            validate_report(report)
        report = self._valid_report()
        del report["negative_claim_ids"]
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_result_signature_binds_report_identity(self):
        report = self._valid_report()
        changed = dict(report)
        changed["dataset"] = "other-dataset"
        changed["harness_version"] = "other-harness"
        self.assertNotEqual(result_signature(report), result_signature(changed))
        changed["suite_version"] = "v2"
        self.assertNotEqual(result_signature(report), result_signature(changed))

    def test_validate_report_rejects_overlapping_claim_arrays(self):
        report = self._valid_report()
        report["claim_ids"] = ["same-claim"]
        report["negative_claim_ids"] = ["same-claim"]
        report["claims_sha256"] = sha256_text(stable_json({"claim_ids": ["same-claim"], "negative_claim_ids": ["same-claim"]}))
        report["run_fingerprint_sha256"] = run_fingerprint(binary_sha256=report["binary_sha256"], control_profile_sha256=report["control_profile_sha256"], dataset_sha256=report["dataset_sha256"], harness_commit=report["harness_commit"], claims_sha256=report["claims_sha256"])
        report["result_signature_sha256"] = result_signature(report)
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_sha256_file_is_real_content_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.txt"
            path.write_text("vault benchmark", encoding="utf-8")
            self.assertEqual(sha256_file(path), "b912dbd1b3b5c2d8932d5ac9bca25b3d4ffa0c52260808e776092512d5e2bf8f")


if __name__ == "__main__":
    unittest.main()
