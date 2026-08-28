import json
import unittest
from pathlib import Path

from benchmark.longmemeval.series_public import SERIES_REPORT_PATH, load_public_series, validate_public_series


class PublicSeriesTests(unittest.TestCase):
    def test_series_is_hash_bound_and_complete(self):
        series = load_public_series()
        self.assertEqual(series["quality_cells_across_runs"], 6000)
        self.assertEqual([run["ordinal"] for run in series["runs"]], [1, 2, 3])
        self.assertEqual(series["aggregate"]["perseus-vault"]["pooled_correct"], 1213)
        self.assertEqual(series["aggregate"]["perseus-vault"]["pooled_n"], 1500)
        self.assertAlmostEqual(series["aggregate"]["perseus-vault"]["pooled_accuracy"], 1213 / 1500, places=4)
        self.assertEqual(series["common_protocol"]["answer_prompt"], "official-cot")
        self.assertEqual(series["common_protocol"]["retrieval"]["k"], 10)

    def test_series_contains_no_raw_payload_fields(self):
        series = load_public_series()
        serialized = json.dumps(series, sort_keys=True)
        for marker in ("payload-must-not-land", "Bearer ", "sk-"):
            self.assertNotIn(marker, serialized)
        with self.assertRaises(ValueError):
            validate_public_series({**series, "raw_response": "forbidden"})

    def test_series_source_artifacts_are_hash_bound(self):
        series = load_public_series()
        for run in series["runs"]:
            self.assertRegex(run["source"]["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(run["source"]["verdict_signature_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(run["source"]["n_attempted"], 2000)
            self.assertEqual(run["source"]["n_graded"], 2000)
            self.assertEqual(run["source"]["answer_errors"], 0)
            self.assertEqual(run["source"]["judge_errors"], 0)

    def test_public_file_is_the_expected_repo_artifact(self):
        self.assertTrue(Path(SERIES_REPORT_PATH).is_file())


if __name__ == "__main__":
    unittest.main()
