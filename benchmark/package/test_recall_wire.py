import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from benchmark.package.common import replay


class RecallWireContractTests(unittest.TestCase):
    def _response(self):
        return {
            "items": [
                {
                    "key": "session-a",
                    "body_json": {"note": "first"},
                    "decay_score": 0.1,
                    "why_served": {"reason": "matched"},
                },
                {
                    "key": "session-b",
                    "body_json": {"note": "second"},
                    "decay_score": 0.9,
                    "score": 0.75,
                    "score_semantics": "semantic-relevance-v1",
                    "why_served": {"reason": "matched"},
                },
            ],
            "total": 2,
            "retrieval_profile": "hybrid",
        }

    def test_valid_response_adds_one_based_wire_rank_without_reordering(self):
        self.assertTrue(hasattr(replay, "normalize_recall_response"))
        result = replay.normalize_recall_response(self._response(), limit=2)
        self.assertEqual(result["schema_version"], replay.RECALL_WIRE_SCHEMA_VERSION)
        self.assertEqual(result["status"], "complete")
        self.assertEqual([item["key"] for item in result["items"]], ["session-a", "session-b"])
        self.assertEqual([item["wire_rank"] for item in result["items"]], [1, 2])
        self.assertNotIn("score", result["items"][0])
        self.assertEqual(result["items"][1]["score"], 0.75)
        self.assertEqual(result["items"][0]["decay_score"], 0.1)

    def test_decay_is_not_used_as_a_relevance_score(self):
        result = replay.normalize_recall_response(self._response(), limit=2)
        self.assertNotIn("score", result["items"][0])
        self.assertEqual(result["items"][1]["score_semantics"], "semantic-relevance-v1")
        self.assertEqual([item["wire_rank"] for item in result["items"]], [1, 2])

    def test_missing_or_null_score_stays_absent(self):
        response = self._response()
        response["items"][1]["score"] = None
        response["items"][1].pop("score_semantics")
        result = replay.normalize_recall_response(response, limit=2)
        self.assertNotIn("score", result["items"][1])
        self.assertNotIn("score_semantics", result["items"][1])

    def test_malformed_shape_becomes_unavailable_without_plausible_items(self):
        malformed = self._response()
        malformed["items"] = [{"key": "good"}, "not-an-item"]
        result = replay.normalize_recall_response(malformed, limit=2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "malformed_recall_response")
        self.assertEqual(result["items"], [])

    def test_known_optional_projection_fields_remain_conformant(self):
        response = self._response()
        response.update({
            "evidence": {"status": "available"},
            "declared_graph": {"nodes": [], "edges": []},
            "freshness_summary": {"fresh": 2, "expired": 0, "never_verified": 0},
        })
        result = replay.normalize_recall_response(response, limit=2)
        self.assertEqual(result["status"], "complete")

    def test_unknown_top_level_shape_becomes_unavailable(self):
        malformed = self._response()
        malformed["confidence"] = 0.99
        result = replay.normalize_recall_response(malformed, limit=2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_nonfinite_or_non_numeric_scores_become_unavailable(self):
        for value in (float("nan"), float("inf"), "0.5", True):
            malformed = self._response()
            malformed["items"][0]["score"] = value
            malformed["items"][0]["score_semantics"] = "semantic-relevance-v1"
            result = replay.normalize_recall_response(malformed, limit=2)
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["items"], [])

    def test_versioned_fixture_covers_current_recall_fields(self):
        fixture = json.loads(
            (Path(__file__).resolve().parent / "recall_wire_fixture.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["schema_version"], replay.RECALL_WIRE_SCHEMA_VERSION)
        response = fixture["response"]
        self.assertIn("items", response)
        self.assertIn("decay_score", response["items"][0])
        self.assertIn("why_served", response["items"][0])
        self.assertIn("retrieval_profile", response)
        normalized = replay.normalize_recall_response(response, limit=1)
        self.assertEqual(normalized["status"], "complete")

    def test_required_items_rejects_unavailable_without_empty_success(self):
        with self.assertRaises(replay.ReplayValidationError):
            replay.require_recall_items({"status": "unavailable"}, limit=2)

    def test_preflight_requires_a_fresh_database_and_binds_all_commitments(self):
        self.assertTrue(hasattr(replay, "prepare_recall_preflight"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / "perseus-vault"
            binary.write_bytes(b"synthetic-binary")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            db = root / "run.db"
            db.write_text("stale", encoding="utf-8")
            (root / "run.db-wal").write_text("stale", encoding="utf-8")
            result = replay.prepare_recall_preflight(
                binary=str(binary),
                db_path=str(db),
                dataset={"name": "fixture", "queries": []},
                config={"limit": 2},
                repo_root=str(Path(__file__).resolve().parents[2]),
            )
            self.assertTrue(result["database_fresh"])
            self.assertEqual(result["database_path"], str(db.resolve()))
            self.assertEqual(len(result["binary_sha256"]), 64)
            self.assertEqual(len(result["binary_commit"]), 40)
            self.assertEqual(len(result["binary_commit_sha256"]), 64)
            self.assertEqual(len(result["dataset_sha256"]), 64)
            self.assertEqual(len(result["config_sha256"]), 64)
            self.assertEqual(result["response_schema"], replay.RECALL_WIRE_SCHEMA_VERSION)
            self.assertEqual(len(result["response_schema_sha256"]), 64)
            self.assertFalse(db.exists())
            self.assertFalse((root / "run.db-wal").exists())


if __name__ == "__main__":
    unittest.main()
