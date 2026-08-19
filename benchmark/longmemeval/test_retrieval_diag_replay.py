import json
import unittest

from benchmark.longmemeval.retrieval_diag import _make_replay_artifact, _make_sufficiency_report
from benchmark.package.common.replay import replay_envelope


class LongMemEvalReplayTests(unittest.TestCase):
    def test_sufficiency_projection_is_composed_hash_only(self):
        report = _make_sufficiency_report(
            [
                {
                    "question_id": "q1",
                    "question_type": "multi-session",
                    "gold": ["gold-a", "gold-b"],
                    "update_gold": None,
                    "ranks": {"gold-a": 1, "gold-b": 3},
                }
            ],
            depth=5,
            dataset_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
        )
        self.assertEqual(report["ks"], [1, 3, 5])
        self.assertEqual(report["strata"]["overall"]["curves"]["all_required_coverage_at_k"]["@3"]["numerator"], 1)
        self.assertNotIn("gold-a", json.dumps(report, sort_keys=True))
        self.assertNotIn("gold-b", json.dumps(report, sort_keys=True))

        instance = {
            "question": "Which workshop was first?",
            "haystack_session_ids": ["s2", "s1"],
            "haystack_dates": ["2023/04/21 (Fri) 00:00", "2023/04/20 (Thu) 00:00"],
        }
        envelope, snapshot = _make_replay_artifact(
            instance,
            "q1",
            [
                {"key": "s2", "body_json": {"note": "second"}, "score": 0.4},
                {"key": "s1", "body_json": {"note": "first"}},
            ],
            2,
            split="s",
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
        )
        self.assertEqual(envelope["status"], "complete")
        self.assertNotIn("second", str(envelope))
        self.assertNotIn("note", str(envelope))
        replayed = replay_envelope(envelope, snapshot)
        self.assertEqual(replayed["candidate_count"], 2)
        self.assertEqual(replayed["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])


if __name__ == "__main__":
    unittest.main()
