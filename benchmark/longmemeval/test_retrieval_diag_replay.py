import inspect
import json
import unittest
from pathlib import Path

from benchmark.longmemeval.retrieval_diag import (
    _make_replay_artifact,
    _make_sufficiency_report,
    _rank_depth_buckets,
    _replay_rows,
    gold_ranks,
    main,
    stable_ranked_items,
)
from benchmark.package.common import replay as replay_module
from benchmark.package.common.replay import replay_envelope


def _fixture_preflight():
    identity = {"device": 1, "inode": 2, "ctime_ns": 3, "size": 4}
    return {
        "binary_sha256": "c" * 64,
        "binary_commit": "d" * 40,
        "binary_commit_sha256": replay_module.sha256_text("d" * 40),
        "database_fresh": True,
        "database_identity": identity,
        "database_id_sha256": replay_module.sha256_text(replay_module.stable_json(identity)),
        "response_schema": replay_module.RECALL_WIRE_SCHEMA_VERSION,
        "response_schema_sha256": replay_module.sha256_text(replay_module.RECALL_WIRE_SCHEMA_VERSION),
        "dataset_sha256": "a" * 64,
        "config_sha256": "b" * 64,
    }


class LongMemEvalReplayTests(unittest.TestCase):
    def test_retrieval_diagnostic_disables_recall_side_effects(self):
        source = inspect.getsource(gold_ranks)
        self.assertIn('"skip_side_effects": True', source)

    def test_equal_provider_scores_use_a_stable_source_key_tiebreak_in_named_rerank(self):
        items = [
            {"key": "session-b", "score": 0.5, "score_semantics": "fixture-relevance-v1"},
            {"key": "session-c", "score": 0.9, "score_semantics": "fixture-relevance-v1"},
            {"key": "session-a", "score": 0.5, "score_semantics": "fixture-relevance-v1"},
        ]
        ordered = stable_ranked_items(items, rerank=True)
        self.assertEqual([item["key"] for item in ordered], ["session-c", "session-a", "session-b"])
        self.assertEqual(stable_ranked_items(list(reversed(items)), rerank=True), ordered)

    def test_explicit_score_does_not_override_wire_order_without_named_rerank(self):
        items = [
            {"key": "wire-b", "wire_rank": 1, "score": 0.5, "score_semantics": "fixture-relevance-v1"},
            {"key": "wire-c", "wire_rank": 2, "score": 0.9, "score_semantics": "fixture-relevance-v1"},
            {"key": "wire-a", "wire_rank": 3, "score": 0.5, "score_semantics": "fixture-relevance-v1"},
        ]
        ordered = stable_ranked_items(items)
        self.assertEqual([item["key"] for item in ordered], ["wire-b", "wire-c", "wire-a"])
        self.assertEqual(
            [item["key"] for item in stable_ranked_items(items, rerank=True)],
            ["wire-c", "wire-a", "wire-b"],
        )

    def test_decay_score_never_reorders_wire_results(self):
        items = [
            {"key": "wire-a", "wire_rank": 1, "decay_score": 0.1},
            {"key": "wire-b", "wire_rank": 2, "decay_score": 0.9},
        ]
        ordered = stable_ranked_items(items, "wire query")
        self.assertEqual([item["key"] for item in ordered], ["wire-a", "wire-b"])

    def test_rank_depth_buckets_treat_ranks_beyond_requested_k_as_hard(self):
        records = [
            {"question_id": "q-over", "question_type": "multi-session", "gold": ["g"], "ranks": {"g": 21}},
            {"question_id": "q-mid", "question_type": "multi-session", "gold": ["g"], "ranks": {"g": 11}},
            {"question_id": "q-absent", "question_type": "multi-session", "gold": ["g"], "ranks": {"g": None}},
        ]
        recoverable, hard = _rank_depth_buckets(records, 20)
        self.assertEqual([row["question_id"] for row in recoverable], ["q-mid"])
        self.assertEqual(hard, ["q-absent", "q-over"])

        inst = {
            "haystack_session_ids": ["s1"],
            "haystack_sessions": [[{"role": "user", "content": "stable fact"}]],
            "haystack_dates": ["2023/04/20 (Thu) 00:00"],
        }
        item_a = {"key": "s1", "body_json": {"note": "stable fact", "created_at_unix_ms": 1}}
        item_b = {"key": "s1", "body_json": {"note": "stable fact", "created_at_unix_ms": 2}}
        self.assertEqual(_replay_rows(inst, [item_a]), _replay_rows(inst, [item_b]))

        items = [
            {"key": "session-a", "score": 0.5, "score_semantics": "fixture-relevance-v1", "body_json": {"note": "unrelated travel plans"}},
            {"key": "session-z", "score": 0.5, "score_semantics": "fixture-relevance-v1", "body_json": {"note": "blue bicycle decision"}},
        ]
        ordered = stable_ranked_items(items, "What did I decide about the blue bicycle?", rerank=True)
        self.assertEqual([item["key"] for item in ordered], ["session-z", "session-a"])

        class OrderedServer:
            def __init__(self, items):
                self.items = items
                self.recall_args = None

            def call(self, name, args):
                if name == "perseus_vault_journal":
                    return {"id": "source-event"}
                if name == "perseus_vault_remember":
                    return {"serveable": True, "proposed": False}
                if name == "perseus_vault_embed":
                    return {"attempted": 3, "embedded": 3, "failed": 0, "errors": 0}
                if name == "perseus_vault_recall":
                    self.recall_args = args
                    return {"items": self.items, "total": len(self.items), "retrieval_profile": "hybrid"}
                raise AssertionError(name)

        inst = {
            "question": "Which fact?",
            "haystack_session_ids": ["s1", "s2", "s3"],
            "haystack_sessions": [[{"role": "user", "content": "fact"}]] * 3,
            "haystack_dates": ["2023/04/20 (Thu) 00:00"] * 3,
            "answer_session_ids": ["s1"],
        }
        items_a = [{"key": "s2", "body_json": {"note": "s2"}, "score": 0.5, "score_semantics": "fixture-relevance-v1"}, {"key": "s1", "body_json": {"note": "s1"}, "score": 0.5, "score_semantics": "fixture-relevance-v1"}, {"key": "s3", "body_json": {"note": "s3"}, "score": 0.5, "score_semantics": "fixture-relevance-v1"}]
        items_b = list(reversed(items_a))
        server_a = OrderedServer(items_a)
        server_b = OrderedServer(items_b)
        result_a = gold_ranks(inst, server_a, "q1", 2, corpus_sha256="a" * 64, config_sha256="b" * 64, code_sha256="c" * 64, preflight=_fixture_preflight())
        result_b = gold_ranks(inst, server_b, "q1", 2, corpus_sha256="a" * 64, config_sha256="b" * 64, code_sha256="c" * 64, preflight=_fixture_preflight())
        ranks_a, _count_a, _update_a, _replay_a, _snapshot_a, _status_a = result_a
        ranks_b, _count_b, _update_b, _replay_b, _snapshot_b, _status_b = result_b
        self.assertEqual(server_a.recall_args["limit"], 3)
        self.assertEqual(server_b.recall_args["limit"], 3)
        self.assertEqual(ranks_a, {"s1": 2})
        self.assertEqual(ranks_b, {"s1": 2})

    def test_malformed_recall_is_unavailable_not_empty_success(self):
        class MalformedServer:
            def call(self, name, args):
                if name == "perseus_vault_journal":
                    return {"id": "source-event"}
                if name == "perseus_vault_remember":
                    return {"serveable": True, "proposed": False}
                if name == "perseus_vault_embed":
                    return {"attempted": 1, "embedded": 1, "failed": 0, "errors": 0}
                if name == "perseus_vault_recall":
                    return {"items": ["malformed"], "total": 1, "retrieval_profile": "hybrid"}
                raise AssertionError(name)

        ranks, _count, _update, envelope, _snapshot, _status = gold_ranks(
            {
                "question": "Which fact?",
                "haystack_session_ids": ["s1"],
                "haystack_sessions": [[{"role": "user", "content": "fact"}]],
                "haystack_dates": ["2023/04/20 (Thu) 00:00"],
                "answer_session_ids": ["s1"],
            },
            MalformedServer(),
            "q-malformed",
            1,
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
            preflight=_fixture_preflight(),
        )
        self.assertEqual(ranks, {"s1": None})
        self.assertEqual(envelope["status"], "unavailable")
        self.assertEqual(envelope["reason"], "malformed_recall_response")

    def test_benchmark_runners_use_fresh_binary_preflight(self):
        from benchmark.longmemeval import run as longmemeval_run
        from benchmark.recall import run as recall_run

        self.assertIn("prepare_recall_preflight", inspect.getsource(recall_run.main))
        self.assertIn("prepare_recall_preflight", inspect.getsource(longmemeval_run.main))

    def test_all_recall_benchmark_consumers_use_wire_normalizer(self):
        root = Path(__file__).resolve().parents[1]
        sources = [
            root / "longmemeval" / "qa.py",
            root / "longmemeval" / "expansion_date_diag.py",
            root / "recall" / "depth_sweep.py",
            root / "recall" / "gate.py",
        ]
        for source_path in sources:
            self.assertIn("normalize_recall_response", source_path.read_text(encoding="utf-8"))

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
                    return {"items": [{"key": "s1", "body_json": {"note": "fixture"}, "score": 1.0, "score_semantics": "fixture-relevance-v1"}], "total": 1, "retrieval_profile": "hybrid"}
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
            preflight=_fixture_preflight(),
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
                {"key": "s2", "body_json": {"note": "second"}, "score": 0.4, "score_semantics": "fixture-relevance-v1"},
                {"key": "s1", "body_json": {"note": "first"}},
            ],
            2,
            split="s",
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
            code_sha256="c" * 64,
            preflight=_fixture_preflight(),
        )
        self.assertEqual(envelope["status"], "complete")
        self.assertNotIn("second", str(envelope))
        self.assertNotIn("note", str(envelope))
        replayed = replay_envelope(envelope, snapshot)
        self.assertEqual(replayed["candidate_count"], 2)
        self.assertEqual(replayed["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])


if __name__ == "__main__":
    unittest.main()
