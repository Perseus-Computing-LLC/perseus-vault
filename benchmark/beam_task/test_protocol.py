"""Contract tests for the official BEAM task lane.

These tests intentionally run without a Vault binary, provider, network, or
LLM.  The actual-data reader and the provider-free fixture share the same
protocol boundary used by the later Vault 100K run.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from benchmark.beam_task import protocol
from benchmark.package.common.replay import replay_envelope


HERE = Path(__file__).resolve().parent
FIXTURE_ROOT = HERE / "fixture"


class BeamTaskProtocolTests(unittest.TestCase):
    def test_fixture_uses_official_layout_and_stable_cases(self):
        first = protocol.load_cases(FIXTURE_ROOT, size="100K")
        second = protocol.load_cases(FIXTURE_ROOT, size="100K")

        self.assertEqual([case["question_id"] for case in first],
                         [case["question_id"] for case in second])
        self.assertEqual({case["ability"] for case in first},
                         {"information_extraction", "abstention"})
        self.assertTrue(all(case["messages"] for case in first))
        self.assertTrue(all(case["rubric"] for case in first))

    def test_loader_accepts_a_checkout_root_with_nested_chats_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "BEAM"
            shutil.copytree(FIXTURE_ROOT, checkout / "chats")
            cases = protocol.load_cases(checkout, size="100K")
            self.assertEqual(len(cases), 2)


    def test_manifest_rejects_floating_dataset_revision(self):
        with self.assertRaisesRegex(ValueError, "revision"):
            protocol.build_manifest(
                data_root=FIXTURE_ROOT,
                sizes=["100K"],
                source_revision="main",
                retrieval={"mode": "hybrid", "top_k": 5},
                answerer=protocol.NOT_MEASURED_MODEL,
                judge=protocol.NOT_MEASURED_MODEL,
            )

    def test_manifest_binds_selected_source_files_and_revision(self):
        manifest = protocol.build_manifest(
            data_root=FIXTURE_ROOT,
            sizes=["100K"],
            source_revision="a" * 40,
            retrieval={"mode": "hybrid", "top_k": 5},
            answerer=protocol.NOT_MEASURED_MODEL,
            judge=protocol.NOT_MEASURED_MODEL,
        )

        self.assertEqual(manifest["schema_version"], protocol.PROTOCOL_SCHEMA)
        self.assertEqual(manifest["source"]["revision"], "a" * 40)
        self.assertEqual(len(manifest["source_files"]), 2)
        self.assertEqual(len(protocol.digest_manifest(manifest)), 64)

    def test_rubric_only_abilities_do_not_require_a_fake_gold_answer(self):
        case = protocol.normalize_question(
            size="100K",
            conversation_id="fixture-1",
            ability="instruction_following",
            index=0,
            raw={
                "question": "Show a code example.",
                "expected_compliance": "Use a fenced code block.",
                "source_chat_ids": [0],
                "rubric": ["The response contains a fenced code block."],
            },
            message_ids={0},
        )
        self.assertIsNone(case["gold"])


    def test_split_leakage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "source message"):
            protocol.normalize_question(
                size="100K",
                conversation_id="fixture-1",
                ability="information_extraction",
                index=0,
                raw={
                    "question": "What is the answer?",
                    "answer": "answer",
                    "source_chat_ids": [999],
                    "rubric": ["answer"],
                },
                message_ids={1, 2},
            )

    def test_measured_model_requires_prompt_identity(self):
        with self.assertRaisesRegex(ValueError, "prompt"):
            protocol.validate_run_config({
                "retrieval": {"mode": "hybrid", "top_k": 5},
                "answerer": {"status": "measured", "model": "model-v1"},
                "judge": protocol.NOT_MEASURED_MODEL,
                "retry_policy": {"max_attempts": 2},
            })

    def test_incomplete_retrieval_artifact_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "complete"):
            protocol.validate_retrieval_artifact({
                "complete": False,
                "ranked": [],
                "top_k": 5,
            })

    def test_retrieval_artifact_rejects_wire_rank_permutation(self):
        with self.assertRaisesRegex(ValueError, "wire_rank"):
            protocol.make_retrieval_snapshot([
                {
                    "key": "first",
                    "content": "first",
                    "wire_rank": 2,
                },
                {
                    "key": "second",
                    "content": "second",
                    "wire_rank": 1,
                },
            ])

    def test_retry_policy_records_terminal_error_without_retrying_forever(self):
        attempts = []

        def failing_call():
            attempts.append(len(attempts) + 1)
            raise RuntimeError("provider unavailable")

        result = protocol.call_with_retries(failing_call, max_attempts=3)
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["error_class"], "RuntimeError")

    def test_vault_adapter_does_not_turn_unusable_rows_into_empty_success(self):
        from benchmark.beam_task import runner

        class FakeServer:
            def __init__(self, binary, db):
                pass

            def call(self, name, args):
                self.last_call = name
                return {
                    "items": [{"key": "message-1", "body_json": {}}],
                    "total": 1,
                    "retrieval_profile": "hybrid",
                }

            def close(self):
                pass

        original = runner.MCPServer
        runner.MCPServer = FakeServer
        try:
            adapter = runner.VaultAdapter("binary", "db", category="fixture", mode="hybrid")
            with self.assertRaises(ValueError):
                adapter.retrieve("question", top_k=1)
            adapter.close()
        finally:
            runner.MCPServer = original

    def test_fixture_adapter_retrieval_is_deterministic(self):
        cases = protocol.load_cases(FIXTURE_ROOT, size="100K")
        adapter = protocol.FixtureAdapter(cases[0]["messages"])
        first = adapter.retrieve(cases[0]["question"], top_k=2)
        second = adapter.retrieve(cases[0]["question"], top_k=2)
        self.assertEqual(first, second)
        self.assertTrue(first)
        self.assertEqual([item["rank"] for item in first], [1, 2][:len(first)])

    def test_healthy_small_population_uses_effective_top_k(self):
        case = protocol.load_cases(FIXTURE_ROOT, size="100K")[0]
        artifact = protocol.make_retrieval_artifact(
            case,
            [{"key": "message-1", "content": "fact"}],
            top_k=5,
        )
        self.assertEqual(artifact["status"], "complete")
        self.assertEqual(artifact["membership"]["requested_top_k"], 1)

    def test_public_projection_is_hash_only_and_deterministic(self):
        case = protocol.load_cases(FIXTURE_ROOT, size="100K")[0]
        artifact = protocol.make_retrieval_artifact(
            case,
            [{"key": "message-1", "score": 0.9, "score_semantics": "fixture-overlap-v1", "content": "private body"}],
            top_k=5,
        )
        first = protocol.project_case(case, artifact)
        second = protocol.project_case(case, artifact)
        self.assertEqual(first, second)
        self.assertNotIn("question", first)
        self.assertNotIn("gold", first)
        self.assertNotIn("private body", json.dumps(first))
        self.assertEqual(len(first["retrieval"]["candidates"][0]["content_sha256"]), 64)

    def test_report_rejects_unvalidated_preflight_projection(self):
        preflight = protocol._fixture_preflight(
            corpus_sha256="a" * 64,
            config_sha256="b" * 64,
        )
        preflight["RAW-QUERY-SENTINEL"] = "must-not-cross"
        with self.assertRaises(ValueError):
            protocol.build_retrieval_report(
                manifest={"schema_version": protocol.PROTOCOL_SCHEMA, "source": {"revision": "a" * 40}},
                config=protocol.default_run_config(),
                cases=[{"question_id": "q1", "ability": "abstention", "status": "not_measured"}],
                evidence_classes={
                    "vault_measured": {"status": "not_measured"},
                    "competitor_published": {"status": "published", "source": "external"},
                    "competitor_reproduced": {"status": "not_measured"},
                },
                preflight=preflight,
            )

    def test_report_projection_keeps_evidence_classes_separate(self):
        report = protocol.build_retrieval_report(
            manifest={"schema_version": protocol.PROTOCOL_SCHEMA, "source": {"revision": "a" * 40}},
            config=protocol.default_run_config(),
            cases=[{"question_id": "q1", "ability": "abstention", "status": "not_measured"}],
            evidence_classes={
                "vault_measured": {"status": "not_measured"},
                "competitor_published": {"status": "published", "source": "external"},
                "competitor_reproduced": {"status": "not_measured"},
            },
        )
        self.assertEqual(report["evidence_classes"]["competitor_published"]["status"], "published")
        self.assertEqual(report["evidence_classes"]["competitor_reproduced"]["status"], "not_measured")
        self.assertEqual(report["raw_inputs_captured"], False)
        self.assertEqual(report["manifest"]["source"]["revision"], "a" * 40)
        self.assertEqual(len(report["custody_sha256"]), 64)

    def test_fixture_runner_writes_hash_only_report_and_replay(self):
        from benchmark.beam_task import runner

        with tempfile.TemporaryDirectory() as directory:
            first = runner.run_dataset(
                data_root=FIXTURE_ROOT,
                sizes=["100K"],
                source_revision="a" * 40,
                config=protocol.default_run_config(),
                adapter_name="fixture",
                output_dir=Path(directory) / "first",
            )
            second = runner.run_dataset(
                data_root=FIXTURE_ROOT,
                sizes=["100K"],
                source_revision="a" * 40,
                config=protocol.default_run_config(),
                adapter_name="fixture",
                output_dir=Path(directory) / "second",
            )

            self.assertEqual(first["custody_sha256"], second["custody_sha256"])
            self.assertEqual(first["status"], "retrieved")
            self.assertEqual(len(first["cases"]), 2)
            self.assertEqual(first["raw_inputs_captured"], False)
            self.assertEqual(first["config"]["answerer"]["model"], "not_measured")
            self.assertEqual(first["config"]["answerer"]["prompt_id"], "not_measured")
            self.assertEqual(first["config"]["judge"]["prompt_id"], "not_measured")
            replay_lines = [json.loads(line) for line in (Path(directory) / "first" / "retrieval_replay.jsonl").read_text().splitlines()]
            snapshot_lines = [json.loads(line) for line in (Path(directory) / "first" / "retrieval_snapshot.jsonl").read_text().splitlines()]
            self.assertEqual(len(replay_lines), 2)
            self.assertEqual(len(snapshot_lines), 2)
            self.assertNotIn("Atlas", json.dumps(replay_lines + snapshot_lines))
            for envelope, snapshot_row in zip(replay_lines, snapshot_lines):
                replayed = replay_envelope(envelope, snapshot_row["snapshot"], allow_synthetic=True)
                self.assertEqual(replayed["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])
            self.assertTrue((Path(directory) / "first" / "report.json").is_file())



if __name__ == "__main__":
    unittest.main()
