import os
import tempfile
import unittest
from pathlib import Path

from benchmark.package.common.publication import build_common_report


class PublicationTests(unittest.TestCase):
    def test_common_envelope_binds_binary_dataset_profile_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            report = build_common_report(
                suite_id="suite",
                suite_version="v1",
                raw_report={
                    "passed": True,
                    "capabilities": {"runner": {"status": "available"}},
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"count": 1}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1", "mode": "offline"},
                repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                claim_ids=[],
                negative_claim_ids=[],
            )
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["public_evidence"], "hash-only")
            self.assertEqual(len(report["run_fingerprint_sha256"]), 64)
            self.assertRegex(report["harness_commit"], r"[0-9a-f]{40}")

    def test_common_envelope_does_not_copy_raw_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            report = build_common_report(
                suite_id="suite",
                suite_version="v1",
                raw_report={
                    "passed": True,
                    "capabilities": {"runner": {"status": "available"}},
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"query": "private", "target_key": "secret", "count": 1}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1"},
                repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                claim_ids=[],
                negative_claim_ids=[],
            )
            self.assertNotIn("query", report["cases"][0]["evidence"])
            self.assertNotIn("target_key", report["cases"][0]["evidence"])

    def test_common_envelope_redacts_private_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            report = build_common_report(
                suite_id="suite",
                suite_version="v1",
                raw_report={
                    "passed": True,
                    "capabilities": {"runner": {"status": "available"}},
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"reason": "private-query-token"}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1"},
                repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                claim_ids=[],
                negative_claim_ids=[],
            )
            self.assertNotEqual(report["cases"][0]["evidence"]["reason"], "private-query-token")
            self.assertEqual(len(report["cases"][0]["evidence"]["reason"]), 64)
            self.assertEqual(report["metrics"]["suite"]["rate"], 1.0)

    def test_common_envelope_marks_unavailable_capability_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            report = build_common_report(
                suite_id="suite",
                suite_version="v1",
                raw_report={
                    "passed": True,
                    "capabilities": {"optional": {"status": "unavailable", "reason": "tool_missing"}},
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"count": 1}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1}},
                },
                binary=binary,
                manifest={"suite": "fixture"},
                profile={"suite": "v1"},
                repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                claim_ids=[],
                negative_claim_ids=[],
            )
            self.assertEqual(report["status"], "partial")

    def test_common_envelope_rejects_private_identifier_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            with self.assertRaises(ValueError):
                build_common_report(
                    suite_id="private-query-token",
                    suite_version="v1",
                    raw_report={
                        "passed": True,
                        "capabilities": {"runner": {"status": "available"}},
                        "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"count": 1}}],
                        "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1}},
                    },
                    binary=binary,
                    manifest={"suite": "fixture"},
                    profile={"suite": "v1"},
                    repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                    claim_ids=[],
                    negative_claim_ids=[],
                    )

    def test_common_envelope_rejects_arbitrary_category_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            report = build_common_report(
                suite_id="suite",
                suite_version="v1",
                raw_report={
                    "passed": True,
                    "capabilities": {"runner": {"status": "available"}},
                    "cases": [{"id": "case", "checks": {"ok": True}, "category": "safe-category", "evidence": {"count": 1}}],
                    "metrics": {"suite": {"numerator": 1, "denominator": 1}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1"},
                repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                claim_ids=[],
                negative_claim_ids=[],
            )
            self.assertEqual(report["cases"][0]["category"], "safe-category")

    def test_common_envelope_rejects_unbounded_metric_rates(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            with self.assertRaises(ValueError):
                build_common_report(
                    suite_id="suite",
                    suite_version="v1",
                    raw_report={
                        "cases": [{"id": "case", "checks": {"ok": True}}],
                        "capabilities": {"runner": {"status": "available"}},
                        "metrics": {"suite": {"numerator": 1, "denominator": 1}},
                        "metric_rates": {"suite": {"status": "available", "nested": {"secret": "x"}}},
                    },
                    binary=binary,
                    manifest={"name": "fixture", "version": 1},
                    profile={"suite": "v1"},
                    repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                    claim_ids=[],
                    negative_claim_ids=[],
                    )

    def test_common_envelope_rejects_fractional_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            with self.assertRaises(ValueError):
                build_common_report(
                    suite_id="suite",
                    suite_version="v1",
                    raw_report={
                        "passed": True,
                        "capabilities": {"runner": {"status": "available"}},
                        "network_calls": 1.9,
                        "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"count": 1}}],
                        "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1}},
                    },
                    binary=binary,
                    manifest={"name": "fixture", "version": 1},
                    profile={"suite": "v1"},
                    repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                    claim_ids=[],
                    negative_claim_ids=[],
                    )

    def test_common_envelope_rejects_invalid_metric_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            with self.assertRaises(ValueError):
                build_common_report(
                    suite_id="suite",
                    suite_version="v1",
                    raw_report={
                        "passed": True,
                        "capabilities": {"runner": {"status": "available"}},
                        "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"count": 1}}],
                        "metrics": {"suite": {"status": "available", "numerator": -1, "denominator": 1}},
                    },
                    binary=binary,
                    manifest={"name": "fixture", "version": 1},
                    profile={"suite": "v1"},
                    repo_root=Path(os.environ.get("PERSEUS_TEST_REPO", str(Path(__file__).resolve().parents[2]))),
                    claim_ids=[],
                    negative_claim_ids=[],
                    )



if __name__ == "__main__":
    unittest.main()
