import hashlib
import json
import unittest
from pathlib import Path

from benchmark.hostile_memory_gauntlet.gauntlet.acceptance import accept_run
from benchmark.hostile_memory_gauntlet.gauntlet.evaluator import run_suite
from benchmark.hostile_memory_gauntlet.gauntlet.protocol import canonical_json, validate_case_bundle, validate_manifest
from benchmark.hostile_memory_gauntlet.gauntlet.providers import NaiveProvider, ReferenceProvider


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"


class PublicFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((FIXTURE_DIR / "public_manifest.json").read_text(encoding="utf-8"))
        cls.bundle = json.loads((FIXTURE_DIR / "public_cases.json").read_text(encoding="utf-8"))
        validate_manifest(cls.manifest)
        validate_case_bundle(cls.bundle, max_cases=cls.manifest["config"]["max_cases"])
        cls.case_hash = hashlib.sha256((FIXTURE_DIR / "public_cases.json").read_bytes()).hexdigest()
        cls.manifest_hash = hashlib.sha256(canonical_json(cls.manifest).encode("utf-8")).hexdigest()

    def run_provider(self, provider):
        return run_suite(
            provider,
            self.manifest,
            self.bundle,
            case_file_sha256=self.case_hash,
            manifest_sha256=self.manifest_hash,
            run_id="public-fixture-test",
        )

    def test_reference_control_is_complete_and_release_ready(self):
        run = self.run_provider(ReferenceProvider())
        acceptance = accept_run(self.manifest, self.bundle, run, case_file_sha256=self.case_hash)
        self.assertEqual(run["verdict"], "passed")
        self.assertEqual(run["passed_cases"], 14)
        self.assertEqual(run["passed_probes"], 15)
        self.assertEqual(acceptance["acceptance_status"], "accepted")
        self.assertTrue(acceptance["release_ready"])

    def test_naive_control_is_complete_but_not_release_ready(self):
        run = self.run_provider(NaiveProvider())
        acceptance = accept_run(self.manifest, self.bundle, run, case_file_sha256=self.case_hash)
        self.assertEqual(run["status"], "complete")
        self.assertEqual(acceptance["acceptance_status"], "accepted")
        self.assertFalse(acceptance["release_ready"])
        self.assertLess(run["passed_cases"], run["case_count"])


if __name__ == "__main__":
    unittest.main()
