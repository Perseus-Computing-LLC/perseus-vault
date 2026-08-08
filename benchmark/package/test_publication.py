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
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"count": 1}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1", "mode": "offline"},
                repo_root=directory,
            )
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["public_evidence"], "hash-only")
            self.assertEqual(len(report["run_fingerprint_sha256"]), 64)
            self.assertEqual(report["harness_commit"], "unknown")

    def test_common_envelope_does_not_copy_raw_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "binary"
            binary.write_bytes(b"benchmark-binary")
            report = build_common_report(
                suite_id="suite",
                suite_version="v1",
                raw_report={
                    "passed": True,
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"query": "private", "target_key": "secret"}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1, "rate": 1.0}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1"},
                repo_root=directory,
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
                    "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {"reason": "private-query-token"}}],
                    "metrics": {"suite": {"status": "available", "numerator": 1, "denominator": 1}},
                },
                binary=binary,
                manifest={"name": "fixture", "version": 1},
                profile={"suite": "v1"},
                repo_root=directory,
            )
            self.assertNotIn("reason", report["cases"][0]["evidence"])
            self.assertEqual(report["metrics"]["suite"]["rate"], 1.0)

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
                        "cases": [{"id": "case", "category": "suite", "status": "passed", "checks": {"ok": True}, "evidence": {}}],
                        "metrics": {"suite": {"status": "available", "numerator": -1, "denominator": 1}},
                    },
                    binary=binary,
                    manifest={"name": "fixture", "version": 1},
                    profile={"suite": "v1"},
                    repo_root=directory,
                )



if __name__ == "__main__":
    unittest.main()
