import unittest

from benchmark.package.common import replay as replay_module
from benchmark.package.common.replay import replay_envelope
from benchmark.recall.run import _make_recall_replay
from benchmark.recall.run import _report_signature


def _fixture_preflight():
    identity = {"device": 1, "inode": 2, "ctime_ns": 3, "size": 4}
    return {
        "binary_sha256": "d" * 64,
        "binary_commit": "e" * 40,
        "binary_commit_sha256": replay_module.sha256_text("e" * 40),
        "database_fresh": True,
        "database_identity": identity,
        "database_id_sha256": replay_module.sha256_text(replay_module.stable_json(identity)),
        "response_schema": replay_module.RECALL_WIRE_SCHEMA_VERSION,
        "response_schema_sha256": replay_module.sha256_text(replay_module.RECALL_WIRE_SCHEMA_VERSION),
        "dataset_sha256": "a" * 64,
        "config_sha256": "b" * 64,
    }


class RecallReplayTests(unittest.TestCase):
    def test_recall_replay_preserves_absent_score_and_replays(self):
        envelope, snapshot = _make_recall_replay(
            dataset_name="synthetic-recall",
            query="Which fact is first?",
            mode="hybrid",
            limit=2,
            items=[
                {"key": "b", "body_json": {"note": "second"}, "score": 0.2, "score_semantics": "fixture-relevance-v1"},
                {"key": "a", "body_json": {"note": "first"}},
            ],
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
            preflight=_fixture_preflight(),
        )
        self.assertEqual(envelope["status"], "complete")
        self.assertTrue(any("score" in row for row in envelope["candidates"]))
        self.assertTrue(any("score" not in row for row in envelope["candidates"]))
        self.assertNotIn("second", str(envelope))
        replayed = replay_envelope(envelope, snapshot, allow_synthetic=True)
        self.assertEqual(replayed["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])

    def test_recall_replay_uses_effective_top_k_for_small_population(self):
        envelope, _snapshot = _make_recall_replay(
            dataset_name="synthetic-recall",
            query="Which fact is first?",
            mode="hybrid",
            limit=5,
            items=[{"key": "a", "body_json": {"note": "first"}}],
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
            preflight=_fixture_preflight(),
            status="complete",
        )
        self.assertEqual(envelope["status"], "complete")
        self.assertEqual(envelope["membership"]["requested_top_k"], 1)

    def test_report_signature_binds_every_measured_cell_preflight(self):
        common = {
            "dataset": "fixture",
            "k": [1],
            "modes": ["hybrid"],
            "hints": False,
            "metrics": {"hybrid": {"mrr": 1.0}},
            "scored_counts": {"hybrid": 1},
            "unavailable_counts": {"hybrid": 0},
        }
        first = _report_signature(
            **common,
            preflight_by_cell={"ingest": {"config_sha256": "a" * 64}, "q1:hybrid": {"config_sha256": "b" * 64}},
        )
        second = _report_signature(
            **common,
            preflight_by_cell={"ingest": {"config_sha256": "a" * 64}, "q1:hybrid": {"config_sha256": "c" * 64}},
        )
        self.assertNotEqual(first, second)

    def test_recall_replay_rejects_missing_body(self):
        with self.assertRaises(ValueError):
            _make_recall_replay(
                dataset_name="synthetic-recall",
                query="Which fact?",
                mode="hybrid",
                limit=1,
                items=[{"key": "a"}],
                corpus_sha256="a" * 64,
                config_sha256="b" * 64,
                code_sha256="c" * 64,
                preflight=_fixture_preflight(),
            )


if __name__ == "__main__":
    unittest.main()
