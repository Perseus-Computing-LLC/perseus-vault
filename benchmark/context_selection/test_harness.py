import json
import unittest
from pathlib import Path

from harness import VARIANTS, run_benchmark, verify_report


class ContextSelectionHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dataset = json.loads(Path(__file__).with_name("dataset.json").read_text())
        cls.report, cls.rows = run_benchmark(dataset)

    def test_rows_cover_questions_positions_and_variants(self):
        self.assertEqual(len(self.rows), 2 * 3 * len(VARIANTS))
        self.assertEqual({row["prompt_position"] for row in self.rows}, {"front", "middle", "tail"})
        self.assertEqual({row["variant"] for row in self.rows}, set(VARIANTS))

    def test_provenance_filter_excludes_stale_contradictory_and_cross_workspace(self):
        rows = [
            row
            for row in self.rows
            if row["question_id"] == "q-deploy-current"
            and row["variant"] == "provenance_filtered"
            and row["prompt_position"] == "front"
        ][0]
        self.assertEqual(rows["selected_source_ids"], ["ops-log-2026-01", "ticket-42"])
        self.assertEqual(rows["metrics"]["unsupported_selection_rate"], 0.0)

    def test_full_context_exposes_selection_cost_without_claiming_qa(self):
        row = [
            row
            for row in self.rows
            if row["question_id"] == "q-budget-boundary"
            and row["variant"] == "full_retrieved"
            and row["prompt_position"] == "tail"
        ][0]
        self.assertIn("other-finance-ledger", row["selected_source_ids"])
        self.assertEqual(self.report["metrics"]["end_to_end_qa"]["status"], "not_run")
        self.assertEqual(self.report["metrics"]["retrieval_only"]["status"], "not_run")

    def test_report_is_hash_verifiable_and_does_not_store_query_text(self):
        self.assertTrue(verify_report(self.report, self.rows))
        serialized = json.dumps(self.rows)
        self.assertNotIn("What is the current", serialized)
        self.assertTrue(self.report["negative_controls"]["attention_weights_not_evidence"])


if __name__ == "__main__":
    unittest.main()
