import unittest

from benchmark.longmemeval.retrieval_diag import _make_replay_artifact
from benchmark.package.common.replay import replay_envelope


class LongMemEvalReplayTests(unittest.TestCase):
    def test_diagnostic_replay_artifact_is_hash_only_and_replayable(self):
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
