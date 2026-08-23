import inspect
import json
import unittest

from benchmark.longmemeval.retrieval_diag import (
    _make_replay_artifact,
    _make_sufficiency_report,
    _replay_rows,
    gold_ranks,
    main,
    stable_ranked_items,
)
from benchmark.package.common.replay import replay_envelope


class LongMemEvalReplayTests(unittest.TestCase):
    def test_retrieval_diagnostic_disables_recall_side_effects(self):
        source = inspect.getsource(gold_ranks)
        self.assertIn('"skip_side_effects": True', source)

    def test_equal_provider_scores_use_a_stable_source_key_tiebreak(self):
        items = [
            {"key": "session-b", "score": 0.5},
            {"key": "session-c", "score": 0.9},
            {"key": "session-a", "score": 0.5},
        ]
        ordered = stable_ranked_items(items)
        self.assertEqual([item["key"] for item in ordered], ["session-c", "session-a", "session-b"])
        self.assertEqual(stable_ranked_items(list(reversed(items))), ordered)

    def test_replay_content_uses_fixture_source_not_volatile_provider_metadata(self):
        inst = {
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[{"role": "user", "content": "stable fact"}]],
            "haystack_dates": ["2023/04/20 (Thu) 00:00"],
        }
        item_a = {"key": "s1", "body_json": {"note": "stable fact", "created_at_unix_ms": 1}}
        item_b = {"key": "s1", "body_json": {"note": "stable fact", "created_at_unix_ms": 2}}
        self.assertEqual(_replay_rows(inst, [item_a]), _replay_rows(inst, [item_b]))

        items = [
            {"key": "session-a", "score": 0.5, "body_json": {"note": "unrelated travel plans"}},
            {"key": "session-z", "score": 0.5, "body_json": {"note": "blue bicycle decision"}},
        ]
        ordered = stable_ranked_items(items, "What did I decide about the blue bicycle?")
        self.assertEqual([item["key"] for item in ordered], ["session-z", "session-a"])

        class OrderedServer:
            def __init__(self, items):
                self.items = items

            def call(self, name, args):
                if name == "perseus_vault_journal":
                    return {"id": "source-event"}
                if name == "perseus_vault_remember":
                    return {"serveable": True, "proposed": False}
                if name == "perseus_vault_embed":
                    return {"attempted": 3, "embedded": 3, "failed": 0, "errors": 0}
                if name == "perseus_vault_recall":
                    return {"items": self.items}
                raise AssertionError(name)

        inst = {
            "question": "Which fact?",
            "haystack_session_ids": ["s1", "s2", "s3"],
            "haystack_sessions": [[{"role": "user", "content": "fact"}]] * 3,
            "haystack_dates": ["2023/04/20 (Thu) 00:00"] * 3,
            "answer_session_ids": ["s1"],
        }
        items_a = [{"key": "s2", "score": 0.5}, {"key": "s1", "score": 0.5}, {"key": "s3", "score": 0.5}]
        items_b = list(reversed(items_a))
        ranks_a = gold_ranks(inst, OrderedServer(items_a), "q1", 3)[0]
        ranks_b = gold_ranks(inst, OrderedServer(items_b), "q1", 3)[0]
        self.assertEqual(ranks_a, {"s1": 1})
        self.assertEqual(ranks_b, ranks_a)

    def test_retrieval_diagnostic_disables_fixture_admission_lint(self):
        source = inspect.getsource(main)
        self.assertIn("PERSEUS_VAULT_DISABLE_ADMISSION_LINT", source)

    def test_retrieval_diagnostic_uses_admitted_write_fixture(self):
        class RecordingServer:
            def __init__(self):
                self.calls = []

            def call(self, name, args):
                self.calls.append((name, args))
                if name == "perseus_vault_journal":
                    return {"id": "source-event"}
                if name == "perseus_vault_remember":
                    return {"serveable": True, "proposed": False}
                if name == "perseus_vault_embed":
                    return {"attempted": 1, "embedded": 1, "failed": 0, "errors": 0}
                if name == "perseus_vault_recall":
                    return {"items": [{"key": "s1", "body_json": {"note": "fixture"}, "score": 1.0}]}
                raise AssertionError(name)

        server = RecordingServer()
        gold_ranks(
            {
                "question": "Which fact?",
                "haystack_session_ids": ["s1"],
                "haystack_sessions": [[{"role": "user", "content": "fact"}]],
                "haystack_dates": ["2023/04/20 (Thu) 00:00"],
                "answer_session_ids": ["s1"],
            },
            server,
            "q1",
            1,
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
        )
        remember = next(args for name, args in server.calls if name == "perseus_vault_remember")
        self.assertIn("admission", remember)
        self.assertEqual(remember["workspace_hash"], "perseus-benchmark")
        recall = next(args for name, args in server.calls if name == "perseus_vault_recall")
        self.assertTrue(recall["skip_side_effects"])

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
