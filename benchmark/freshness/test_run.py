import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from run import classify_outcome, freshness_signature, percentile_nearest_rank


class FreshnessHelperTests(unittest.TestCase):
    def test_nearest_rank_percentile_is_deterministic(self):
        values = [30.0, 10.0, 20.0]
        self.assertEqual(percentile_nearest_rank(values, 50), 20.0)
        self.assertEqual(percentile_nearest_rank(values, 95), 30.0)

    def test_outcome_classifier_preserves_explicit_states(self):
        self.assertEqual(classify_outcome({"status": "Stale", "abstained": True}), "stale_abstained")
        self.assertEqual(classify_outcome({"status": "Fresh", "abstained": False}), "fresh")
        self.assertEqual(classify_outcome({}), "missing")

    def test_signature_ignores_runtime_timings_and_order(self):
        rows = [
            {"case": "b", "axis": "write_to_fts", "ok": True, "elapsed_ms": 8.0},
            {"case": "a", "axis": "restart_recall", "ok": False, "elapsed_ms": 11.0},
        ]
        changed = [
            {"case": "a", "axis": "restart_recall", "ok": False, "elapsed_ms": 99.0},
            {"case": "b", "axis": "write_to_fts", "ok": True, "elapsed_ms": 1.0},
        ]
        self.assertEqual(freshness_signature(rows), freshness_signature(changed))
        self.assertEqual(len(freshness_signature(rows)), 64)


if __name__ == "__main__":
    unittest.main()
