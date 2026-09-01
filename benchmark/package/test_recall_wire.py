import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from benchmark.package.common import replay
from benchmark.package.common.replay import validate_recall_preflight


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

    def test_body_json_must_be_a_json_object(self):
        for body in (None, 0, [], "not-json", "null", "[]"):
            response = self._response()
            response["items"][0]["body_json"] = body
            result = replay.normalize_recall_response(response, limit=2)
            self.assertEqual(result["status"], "unavailable", repr(body))
            self.assertEqual(result["items"], [], repr(body))

        response = self._response()
        response["items"][0].pop("body_json")
        result = replay.normalize_recall_response(response, limit=2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_empty_key_does_not_fall_back_to_id(self):
        response = {
            "items": [{"key": "", "id": "fallback", "body_json": {"note": "x"}}],
            "total": 1,
            "retrieval_profile": "fixture",
        }
        result = replay.normalize_recall_response(response, limit=1)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

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

    def test_nested_optional_projection_unknown_fields_fail_closed(self):
        response = self._response()
        response["diagnostic"] = {
            "reason": "no_match",
            "active_memories": 0,
            "RAW-QUERY-SENTINEL": "must-not-cross",
        }
        result = replay.normalize_recall_response(response, limit=2)
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

    def test_query_expansion_envelope_accepts_variants_without_profile(self):
        response = {
            "items": [{"key": "expanded-a", "body_json": {"note": "expanded"}}],
            "total": 1,
            "variants": 2,
        }
        result = replay.normalize_recall_response(response, limit=1)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["variants"], 2)

    def test_numeric_score_without_explicit_semantics_is_unavailable(self):
        response = self._response()
        response["items"][0]["score"] = 0.4
        result = replay.normalize_recall_response(response, limit=2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_unsafe_outcome_statuses_are_unavailable(self):
        for status in ("timeout", "stale", "unknown", "fresh"):
            response = self._response()
            response["outcome"] = {"status": status}
            result = replay.normalize_recall_response(response, limit=2)
            if status == "fresh":
                self.assertEqual(result["status"], "complete")
            else:
                self.assertEqual(result["status"], "unavailable", status)
                self.assertEqual(result["items"], [])

    def test_empty_items_with_positive_total_are_unavailable(self):
        result = replay.normalize_recall_response(
            {"items": [], "total": 2, "retrieval_profile": "hybrid"}, limit=2
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_only_complete_or_empty_results_are_scoreable(self):
        for status in ("complete", "empty"):
            self.assertTrue(replay.recall_status_is_scoreable(status), status)
        for status in ("partial", "degraded", "unavailable", "unknown"):
            self.assertFalse(replay.recall_status_is_scoreable(status), status)

    def test_capitalized_unsafe_outcome_status_is_unavailable(self):
        result = replay.normalize_recall_response(
            {"items": [], "total": 0, "outcome": {"status": "Unavailable"}}, limit=2
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_item_projection_is_closed_and_nested_why_served_is_bounded(self):
        unknown_item = self._response()
        unknown_item["items"][0]["private_projection"] = {"token": "must-not-cross"}
        self.assertEqual(
            replay.normalize_recall_response(unknown_item, limit=2)["status"],
            "unavailable",
        )

        unknown_nested = self._response()
        unknown_nested["items"][0]["why_served"]["raw_query"] = "must-not-cross"
        self.assertEqual(
            replay.normalize_recall_response(unknown_nested, limit=2)["status"],
            "unavailable",
        )

    def test_duplicate_keys_and_inconsistent_wire_ranks_fail_closed(self):
        duplicate = self._response()
        duplicate["items"][1]["key"] = duplicate["items"][0]["key"]
        self.assertEqual(
            replay.normalize_recall_response(duplicate, limit=2)["status"],
            "unavailable",
        )

        wrong_rank = self._response()
        wrong_rank["items"][0]["wire_rank"] = 7
        self.assertEqual(
            replay.normalize_recall_response(wrong_rank, limit=2)["status"],
            "unavailable",
        )

    def test_preflight_runtime_database_identity_is_checked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo_root = Path(__file__).resolve().parents[2]
            commit = subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            binary = root / "perseus-vault"
            binary.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'perseus-vault test (v0.0.0-0-g"
                + commit[:12]
                + ")'\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            db = root / "run.db"
            result = replay.prepare_recall_preflight(
                binary=str(binary),
                db_path=str(db),
                dataset={"name": "fixture", "queries": []},
                config={"limit": 2},
                repo_root=str(repo_root),
            )
            replacement = root / "replacement.db"
            replacement.write_bytes(db.read_bytes())
            with self.assertRaises(replay.ReplayValidationError):
                replay.validate_recall_preflight(
                    result,
                    binary=str(binary),
                    db_path=str(replacement),
                    repo_root=str(repo_root),
                    dataset={"name": "fixture", "queries": []},
                    config={"limit": 2},
                )

    def test_rust_serde_enum_outcomes_are_normalized_without_changing_safety(self):
        for wire_status, expected in (("Empty", "empty"), ("Fresh", "complete")):
            response = self._response()
            if wire_status == "Empty":
                response["items"] = []
                response["total"] = 0
            response["outcome"] = {"status": wire_status}
            result = replay.normalize_recall_response(response, limit=2)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["outcome"]["status"], wire_status.lower())
        response = self._response()
        response["outcome"] = {"status": "Timeout"}
        result = replay.normalize_recall_response(response, limit=2)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], [])

    def test_optional_projection_shapes_are_validated_and_preserved(self):
        response = self._response()
        response.update({
            "evidence": {"status": "available"},
            "declared_graph": {"nodes": [], "edges": []},
            "freshness_summary": {"fresh": 2, "expired": 0, "never_verified": 0},
        })
        result = replay.normalize_recall_response(response, limit=2)
        self.assertEqual(result["evidence"], response["evidence"])
        self.assertEqual(result["declared_graph"], response["declared_graph"])
        malformed = self._response()
        malformed["evidence"] = []
        unavailable = replay.normalize_recall_response(malformed, limit=2)
        self.assertEqual(unavailable["status"], "unavailable")

    def test_variants_must_be_a_nonnegative_integer(self):
        for value in (True, -1, "2"):
            response = {
                "items": [],
                "total": 0,
                "variants": value,
            }
            self.assertEqual(replay.normalize_recall_response(response, limit=2)["status"], "unavailable")

    def test_preflight_requires_a_fresh_database_and_binds_all_commitments(self):
        self.assertTrue(hasattr(replay, "prepare_recall_preflight"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo_root = Path(__file__).resolve().parents[2]
            commit = subprocess.check_output(
                ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            binary = root / "perseus-vault"
            binary.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'perseus-vault test (v0.0.0-0-g"
                + commit[:12]
                + ")'\n",
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            db = root / "run.db"
            db.write_text("stale", encoding="utf-8")
            (root / "run.db-wal").write_text("stale", encoding="utf-8")
            dataset = {"name": "fixture", "queries": []}
            config = {"limit": 2}
            result = replay.prepare_recall_preflight(
                binary=str(binary),
                db_path=str(db),
                dataset=dataset,
                config=config,
                repo_root=str(repo_root),
            )
            self.assertTrue(result["database_fresh"])
            self.assertNotIn("binary_path", result)
            self.assertNotIn("database_path", result)
            self.assertEqual(len(result["binary_sha256"]), 64)
            self.assertEqual(len(result["binary_commit"]), 40)
            self.assertEqual(len(result["binary_commit_sha256"]), 64)
            self.assertEqual(len(result["dataset_sha256"]), 64)
            self.assertEqual(len(result["config_sha256"]), 64)
            self.assertEqual(result["response_schema"], replay.RECALL_WIRE_SCHEMA_VERSION)
            self.assertEqual(len(result["response_schema_sha256"]), 64)
            self.assertTrue(db.exists())
            self.assertEqual(result["database_id_sha256"], replay.sha256_text(replay.stable_json(result["database_identity"])))
            self.assertFalse((root / "run.db-wal").exists())

            validate_recall_preflight(
                result,
                binary=str(binary),
                db_path=str(db),
                repo_root=str(repo_root),
                dataset=dataset,
                config=config,
            )
            binary.write_text(binary.read_text(encoding="utf-8") + "# changed\\n", encoding="utf-8")
            with self.assertRaises(replay.ReplayValidationError):
                validate_recall_preflight(
                    result,
                    binary=str(binary),
                    repo_root=str(repo_root),
                    dataset=dataset,
                    config=config,
                )


if __name__ == "__main__":
    unittest.main()
