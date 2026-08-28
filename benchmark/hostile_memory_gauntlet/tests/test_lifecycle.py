import hashlib
import unittest

from benchmark.hostile_memory_gauntlet.gauntlet.acceptance import accept_run
from benchmark.hostile_memory_gauntlet.gauntlet.evaluator import run_suite
from benchmark.hostile_memory_gauntlet.gauntlet.protocol import canonical_json, content_signature, sha256_text
from benchmark.hostile_memory_gauntlet.gauntlet.providers import ReferenceProvider


class LifecycleTests(unittest.TestCase):
    def _contract(self):
        text = "Alice is an analyst."
        digest = hashlib.sha256(text.encode()).hexdigest()
        bundle = {
            "schema": "perseus-hostile-memory-gauntlet/cases/v1",
            "cases": [{
                "case_id": "case-1", "category": "correction",
                "events": [{"type": "ingest", "expected_status": "admitted", "record": {
                    "record_id": "r1", "memory_key": "profile.alice.role", "scope": "team-a",
                    "text": text, "source_ref": "source://r1", "record_digest": digest,
                    "actor": "alice", "trust": "authoritative", "valid_from": 1,
                    "recorded_at": 1, "status": "active", "supersedes": [],
                }}],
                "probes": [{
                    "probe_id": "p1", "query": "Alice role", "scope": "team-a", "as_of": 2,
                    "limit": 5, "expected": {"decision": "answer", "required_ids": ["r1"],
                    "forbidden_ids": [], "required_scope": "team-a", "require_provenance": True,
                    "max_context_words": 20},
                }],
            }],
        }
        manifest = {
            "schema": "perseus-hostile-memory-gauntlet/manifest/v1",
            "suite_id": "test-suite", "case_ids": ["case-1"],
            "required_categories": ["correction"], "config": {"max_cases": 30},
        }
        return manifest, bundle

    def test_complete_run_is_accepted_and_release_ready(self):
        manifest, bundle = self._contract()
        case_hash = sha256_text("case-file")
        run = run_suite(
            ReferenceProvider(), manifest, bundle,
            case_file_sha256=case_hash,
            manifest_sha256=sha256_text(canonical_json(manifest)),
            run_id="test-run",
        )
        accepted = accept_run(manifest, bundle, run, case_file_sha256=case_hash)
        self.assertEqual(accepted["acceptance_status"], "accepted")
        self.assertTrue(accepted["release_ready"])
        self.assertEqual(run["provider_metadata"]["real_producer"], False)
        self.assertEqual(run["provider_metadata"]["network_calls"], 0)

    def test_tampered_metric_is_rejected_even_with_old_report_shape(self):
        manifest, bundle = self._contract()
        case_hash = sha256_text("case-file")
        run = run_suite(
            ReferenceProvider(), manifest, bundle,
            case_file_sha256=case_hash,
            manifest_sha256=sha256_text(canonical_json(manifest)),
            run_id="test-run",
        )
        run["metrics"]["probe_pass_rate"] = 0.0
        accepted = accept_run(manifest, bundle, run, case_file_sha256=case_hash)
        self.assertEqual(accepted["acceptance_status"], "rejected")
        self.assertFalse(accepted["checks"]["metric_recompute"])
        self.assertFalse(accepted["checks"]["report_signature"])

    def test_malformed_case_results_are_rejected_without_verifier_crash(self):
        manifest, bundle = self._contract()
        case_hash = sha256_text("case-file")
        run = run_suite(
            ReferenceProvider(), manifest, bundle,
            case_file_sha256=case_hash,
            manifest_sha256=sha256_text(canonical_json(manifest)),
            run_id="test-run",
        )
        run["case_results"] = "not-a-list"
        accepted = accept_run(manifest, bundle, run, case_file_sha256=case_hash)
        self.assertEqual(accepted["acceptance_status"], "rejected")
        self.assertFalse(accepted["checks"]["case_ids"])

    def test_missing_provider_identity_is_rejected_even_with_recomputed_signature(self):
        manifest, bundle = self._contract()
        case_hash = sha256_text("case-file")
        run = run_suite(
            ReferenceProvider(), manifest, bundle,
            case_file_sha256=case_hash,
            manifest_sha256=sha256_text(canonical_json(manifest)),
            run_id="test-run",
        )
        run.pop("provider_metadata")
        run["signature_sha256"] = content_signature(run)
        accepted = accept_run(manifest, bundle, run, case_file_sha256=case_hash)
        self.assertEqual(accepted["acceptance_status"], "rejected")
        self.assertFalse(accepted["checks"]["provider_metadata"])


if __name__ == "__main__":
    unittest.main()
