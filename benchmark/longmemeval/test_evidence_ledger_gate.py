"""Provider-free tests for the preregistered evidence-ledger gate."""
import importlib.util
import inspect
import pathlib
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("evidence_ledger_gate", HERE / "evidence_ledger_gate.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("could not load evidence_ledger_gate.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class EvidenceLedgerGateTests(unittest.TestCase):
    def test_gate_evaluates_paired_arms_without_provider_calls(self):
        inst = {
            "question_id": "q1",
            "question_type": "multi-session",
            "question": "How many workshops did I attend?",
            "answer": "3",
            "answer_session_ids": ["s1"],
            "haystack_session_ids": ["s1", "s2"],
            "haystack_dates": ["2023/04/20 (Thu) 00:00", "2023/04/21 (Fri) 00:00"],
            "haystack_sessions": [
                [{"role": "user", "content": "I attended 3 workshops."}],
                [{"role": "user", "content": "I considered a workshop."}],
            ],
        }
        causal = {"bucket": "both_wrong_or_answer_limited", "answer": "3"}
        focus = {"oracle_hypothesis": "I attended 3 workshops."}
        record = GATE.evaluate_case(inst, causal, focus, ["s1", "s2"], budget_tokens=200)
        self.assertEqual(record["question_id"], "q1")
        self.assertEqual(record["stratum"], "both_wrong_or_answer_limited")
        self.assertTrue(record["checks"]["deterministic"])
        self.assertTrue(record["checks"]["budget_ok"])
        self.assertTrue(record["checks"]["source_token_preservation"])
        self.assertEqual(record["provider_calls"], 0)
        self.assertEqual(record["judge_calls"], 0)
        self.assertTrue(record["candidate"]["all_gold"])
        self.assertIn("oracle_hypothesis_token_proxy", record["candidate"])

    def test_causal_strata_require_the_preregistered_63_case_shape(self):
        rows = []
        counts = {
            "both_correct": 18,
            "candidate_gain_over_fullcontext": 15,
            "candidate_regression_vs_fullcontext": 6,
            "both_wrong_or_answer_limited": 24,
        }
        for bucket, n in counts.items():
            rows.extend({"question_id": f"q{i}", "question_type": "multi-session",
                         "bucket": bucket, "candidate_all_gold": True}
                         for i in range(len(rows), len(rows) + n))
        self.assertTrue(GATE.validate_causal_rows(rows))
        rows[-1] = {**rows[-1], "bucket": "unexpected"}
        with self.assertRaises(ValueError):
            GATE.validate_causal_rows(rows)

    def test_gate_is_provider_and_judge_free_by_construction(self):
        source = inspect.getsource(GATE)
        self.assertNotIn("openai", source.lower())
        self.assertIn('"provider_calls": 0', source)
        self.assertIn('"judge_calls": 0', source)


if __name__ == "__main__":
    unittest.main()
