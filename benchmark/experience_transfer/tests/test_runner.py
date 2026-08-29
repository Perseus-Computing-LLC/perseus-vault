from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmark.experience_transfer.common import ContractError, public_report_signature, validate_public_report
from benchmark.experience_transfer.runner import run


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus/corpus.json"


class RunnerContractTests(unittest.TestCase):
    def test_runner_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="vet-run-") as temp:
            first_dir = Path(temp) / "one"
            second_dir = Path(temp) / "two"
            run(CORPUS, first_dir)
            run(CORPUS, second_dir)
            for name in ("manifest.json", "public_report.json", "acceptance_report.json"):
                self.assertEqual((first_dir / name).read_bytes(), (second_dir / name).read_bytes(), name)

    def test_report_is_public_safe_and_hash_bound(self):
        with tempfile.TemporaryDirectory(prefix="vet-report-") as temp:
            outdir = Path(temp)
            result = run(CORPUS, outdir)
            report_path = outdir / "public_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            validate_public_report(report)
            self.assertEqual(report["provider_calls"], 0)
            self.assertEqual(report["judge_calls"], 0)
            self.assertEqual(report["report_signature_sha256"], public_report_signature(report))
            self.assertEqual(result["report_sha256"], hashlib.sha256(report_path.read_bytes()).hexdigest())
            serialized = json.dumps(report, sort_keys=True).lower()
            for marker in ("payload-must-not-land", "bearer ", "sk-", "customer-secret"):
                self.assertNotIn(marker, serialized)
            for key in ("prompt", "context_body", "body_json", "provider_response", "secret", "credential"):
                self.assertNotIn(f'"{key}"', serialized)

    def test_public_report_rejects_forbidden_field_and_bad_metric(self):
        with tempfile.TemporaryDirectory(prefix="vet-report-") as temp:
            outdir = Path(temp)
            run(CORPUS, outdir)
            report = json.loads((outdir / "public_report.json").read_text(encoding="utf-8"))
            broken = copy.deepcopy(report)
            broken["metrics"][0]["denominator"] = 0
            broken["metrics"][0]["rate"] = 1.0
            with self.assertRaises(ContractError):
                validate_public_report(broken)
            broken = copy.deepcopy(report)
            broken["claim_boundary"]["not_supported"].append("prompt")
            # Strings are not fields; introducing a real forbidden field is the
            # boundary test that must fail.
            broken["leaked_prompt"] = "synthetic text"
            with self.assertRaises(ContractError):
                validate_public_report(broken)

    def test_acceptance_binds_manifest_and_report_bytes(self):
        with tempfile.TemporaryDirectory(prefix="vet-acceptance-") as temp:
            outdir = Path(temp)
            run(CORPUS, outdir)
            acceptance = json.loads((outdir / "acceptance_report.json").read_text(encoding="utf-8"))
            self.assertEqual(acceptance["manifest_sha256"], hashlib.sha256((outdir / "manifest.json").read_bytes()).hexdigest())
            self.assertEqual(acceptance["report_sha256"], hashlib.sha256((outdir / "public_report.json").read_bytes()).hexdigest())
            self.assertEqual(acceptance["external_implementation"], "not_measured")
            self.assertEqual(acceptance["status"], "ready_for_provider_free_review")


if __name__ == "__main__":
    unittest.main()
