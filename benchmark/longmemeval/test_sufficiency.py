from __future__ import annotations

import copy
import json
import unittest

from benchmark.longmemeval.sufficiency import (
    SUFFICIENCY_SCHEMA_VERSION,
    SufficiencyError,
    build_sufficiency_report,
    validate_sufficiency_report,
)


class EvidenceSufficiencyTests(unittest.TestCase):
    def _records(self):
        return [
            {
                "question_id": "q-single",
                "question_type": "single-session-user",
                "required_evidence": ["a"],
                "latest_evidence": [],
                "temporal_anchors": [],
                "ranked_ids": ["a", "x", "y"],
                "status": "available",
            },
            {
                "question_id": "q-multi",
                "question_type": "multi-session",
                "required_evidence": ["a", "b"],
                "latest_evidence": [],
                "temporal_anchors": [],
                "ranked_ids": ["a", "x", "b"],
                "status": "available",
            },
            {
                "question_id": "q-temporal",
                "question_type": "temporal-reasoning",
                "required_evidence": ["old", "new"],
                "latest_evidence": ["new"],
                "temporal_anchors": ["old", "new"],
                "stale_evidence": ["old"],
                "ranked_ids": ["old", "x", "new"],
                "status": "available",
            },
            {
                "question_id": "q-unavailable",
                "question_type": "knowledge-update",
                "required_evidence": ["u"],
                "latest_evidence": ["u"],
                "temporal_anchors": [],
                "ranked_ids": None,
                "status": "unavailable",
            },
            {
                "question_id": "q-truncated",
                "question_type": "knowledge-update",
                "required_evidence": ["a", "b"],
                "latest_evidence": ["b"],
                "temporal_anchors": [],
                "ranked_ids": ["a"],
                "status": "truncated",
            },
            {
                "question_id": "q-duplicate",
                "question_type": "multi-session",
                "required_evidence": ["a", "b"],
                "latest_evidence": [],
                "temporal_anchors": [],
                "ranked_ids": ["a", "a", "b"],
                "status": "available",
            },
        ]

    def _report(self, records=None):
        return build_sufficiency_report(
            records or self._records(),
            dataset_sha256="1" * 64,
            fixture_sha256="2" * 64,
            retrieval_config_sha256="3" * 64,
            code_sha256="4" * 64,
            ks=(1, 3, 5, 10, 20, 50),
            focus_strata={
                "multi-evidence": ["multi-session", "knowledge-update"],
                "temporal": ["temporal-reasoning"],
            },
        )

    def test_versioned_report_schema_is_present(self):
        schema = json.loads((__import__("pathlib").Path(__file__).with_name("sufficiency.schema.json")).read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], SUFFICIENCY_SCHEMA_VERSION)
        self.assertIn("projection_sha256", schema["required"])

    def test_expected_curves_keep_single_hit_and_all_required_separate(self):
        report = self._report()
        self.assertEqual(report["schema_version"], SUFFICIENCY_SCHEMA_VERSION)
        overall = report["strata"]["overall"]["curves"]
        self.assertEqual(overall["single_hit_recall_at_k"]["@1"]["numerator"], 3)
        self.assertEqual(overall["single_hit_recall_at_k"]["@1"]["denominator"], 3)
        self.assertEqual(overall["all_required_coverage_at_k"]["@1"]["numerator"], 1)
        self.assertEqual(overall["all_required_coverage_at_k"]["@3"]["numerator"], 3)
        self.assertNotEqual(
            overall["single_hit_recall_at_k"]["@1"]["rate"],
            overall["all_required_coverage_at_k"]["@1"]["rate"],
        )

    def test_latest_temporal_worst_rank_missing_and_stale_are_explicit(self):
        report = self._report()
        overall = report["strata"]["overall"]
        self.assertEqual(overall["curves"]["latest_version_coverage_at_k"]["@1"]["numerator"], 0)
        self.assertEqual(overall["curves"]["latest_version_coverage_at_k"]["@1"]["denominator"], 1)
        self.assertEqual(overall["curves"]["latest_version_coverage_at_k"]["@3"]["numerator"], 1)
        self.assertEqual(overall["curves"]["temporal_anchor_coverage_at_k"]["@1"]["denominator"], 1)
        cases = {case["question_id"]: case for case in report["cases"]}
        self.assertEqual(cases["q-multi"]["worst_required_rank"], 3)
        self.assertEqual(cases["q-truncated"]["missing_required_count"], 1)
        self.assertEqual(cases["q-temporal"]["stale_exposure_count"], 1)
        self.assertEqual(report["status_counts"]["unavailable"], 1)
        self.assertEqual(report["status_counts"]["truncated"], 1)
        self.assertEqual(report["status_counts"]["duplicate"], 1)

    def test_per_type_and_focus_curves_and_projection_are_deterministic(self):
        first = self._report()
        second = self._report(copy.deepcopy(self._records()))
        self.assertEqual(first, second)
        self.assertIn("multi-session", first["strata"]["by_question_type"])
        self.assertIn("multi-evidence", first["strata"]["focus"])
        self.assertTrue(first["projection_sha256"])
        self.assertTrue(first["signature_sha256"])
        self.assertNotIn("old", json.dumps(first, sort_keys=True))
        self.assertNotIn("memory body", json.dumps(first, sort_keys=True))
        validate_sufficiency_report(first)

    def test_duplicate_required_and_missing_structural_fields_fail_closed(self):
        duplicate_required = self._records()[:1]
        duplicate_required[0]["required_evidence"] = ["a", "a"]
        with self.assertRaises(SufficiencyError):
            self._report(duplicate_required)
        missing = self._records()[:1]
        del missing[0]["question_type"]
        with self.assertRaises(SufficiencyError):
            self._report(missing)

    def test_tampered_projection_and_gold_aware_production_fields_are_rejected(self):
        report = self._report()
        tampered = dict(report)
        tampered["projection_sha256"] = "f" * 64
        with self.assertRaises(SufficiencyError):
            validate_sufficiency_report(tampered)
        bad = self._records()[:1]
        bad[0]["gold_answers"] = ["raw answer"]
        with self.assertRaises(SufficiencyError):
            self._report(bad)


if __name__ == "__main__":
    unittest.main()
