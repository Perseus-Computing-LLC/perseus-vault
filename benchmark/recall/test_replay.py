import unittest

from benchmark.package.common.replay import replay_envelope
from benchmark.recall.run import _make_recall_replay


class RecallReplayTests(unittest.TestCase):
    def test_recall_replay_preserves_absent_score_and_replays(self):
        envelope, snapshot = _make_recall_replay(
            dataset_name="synthetic-recall",
            query="Which fact is first?",
            mode="hybrid",
            limit=2,
            items=[
                {"key": "b", "body_json": {"note": "second"}, "score": 0.2},
                {"key": "a", "body_json": {"note": "first"}},
            ],
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
        )
        self.assertEqual(envelope["status"], "complete")
        self.assertTrue(any("score" in row for row in envelope["candidates"]))
        self.assertTrue(any("score" not in row for row in envelope["candidates"]))
        self.assertNotIn("second", str(envelope))
        replayed = replay_envelope(envelope, snapshot)
        self.assertEqual(replayed["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])


if __name__ == "__main__":
    unittest.main()
