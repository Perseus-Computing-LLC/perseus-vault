import json
import unittest

from benchmark.recall.fusion_regression import (
    SCHEMA_VERSION,
    canonical_fingerprint,
    run_conflict_magnet_fixture,
)


class FusionRegressionTests(unittest.TestCase):
    def test_conflict_presentation_never_contaminates_raw_rrf(self):
        report = run_conflict_magnet_fixture()

        self.assertEqual(report["schema_version"], SCHEMA_VERSION)
        self.assertEqual(report["claim_boundary"], "synthetic-ranking-contract-only")
        self.assertEqual(report["network_calls"], 0)
        self.assertEqual(report["raw_inputs_captured"], False)
        self.assertEqual(
            report["fusion"]["fused_ids"], report["fusion"]["pre_presentation_ids"]
        )
        self.assertNotEqual(
            report["bad_control"]["fused_ids"], report["fusion"]["fused_ids"]
        )
        self.assertEqual(
            report["final"]["ids"], report["fusion"]["post_presentation_ids"]
        )
        self.assertEqual(report["final"]["forbidden_neighbor_exposure"], 1)
        self.assertEqual(report["final"]["positive_recall"], 1)

        encoded = json.dumps(report, sort_keys=True)
        for forbidden in ("body", "prompt", "query", "gold", "secret", "token"):
            self.assertNotIn(f'"{forbidden}"', encoded)

    def test_metadata_signal_cannot_promote_weak_raw_match(self):
        report = run_conflict_magnet_fixture()
        adjusted = report["metadata_control"]["adjusted_rank"]
        self.assertLess(adjusted["relevant"], adjusted["weak_metadata"])
        self.assertEqual(report["metadata_control"]["positive_multiplier_cap"], 1.0)
        self.assertEqual(report["metadata_control"]["weak_metadata_multiplier"], 1.0)

    def test_report_is_byte_stable_and_digest_bound(self):
        first = run_conflict_magnet_fixture()
        second = run_conflict_magnet_fixture()
        self.assertEqual(first, second)
        self.assertEqual(first["signature_sha256"], canonical_fingerprint(first))


if __name__ == "__main__":
    unittest.main()
