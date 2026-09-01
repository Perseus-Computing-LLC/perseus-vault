import copy
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from benchmark.longmemeval import retrieval_diag
from benchmark.package.common import replay as replay_module
from benchmark.package.common.replay import (
    SCHEMA_VERSION,
    ReplayValidationError,
    build_envelope,
    build_snapshot,
    replay_envelope,
    validate_envelope,
)


class RetrievalReplayTests(unittest.TestCase):
    def _context(self):
        identity = {"device": 1, "inode": 2, "ctime_ns": 3, "size": 4}
        return {
            "workspace_id": "fixture-workspace",
            "scope": "workspace:fixture",
            "fixture_id": "synthetic-retrieval-v1",
            "corpus_sha256": "a" * 64,
            "retrieval_profile": "hybrid-default",
            "mode": "hybrid",
            "top_k": 2,
            "cell_id": "cell-001",
            "request_sha256": "b" * 64,
            "config_sha256": "c" * 64,
            "code_sha256": "d" * 64,
            "context_policy": "chronological-sequence-v1",
            "context_policy_version": "1",
            "preflight": {
                "binary_sha256": "d" * 64,
                "binary_commit": "e" * 40,
                "binary_commit_sha256": replay_module.sha256_text("e" * 40),
                "database_fresh": True,
                "database_identity": identity,
                "database_id_sha256": replay_module.sha256_text(replay_module.stable_json(identity)),
                "response_schema": replay_module.RECALL_WIRE_SCHEMA_VERSION,
                "response_schema_sha256": replay_module.sha256_text(replay_module.RECALL_WIRE_SCHEMA_VERSION),
                "dataset_sha256": "a" * 64,
                "config_sha256": "c" * 64,
            },
        }

    def _candidates(self):
        return [
            {
                "candidate_id": "candidate-b",
                "source_ref": "session-b/turn-2",
                "content": "second synthetic fact",
                "provenance": "fixture",
                "wire_rank": 1,
                "original_position": 2,
                "score": 0.8,
                "score_semantics": "rrf-v1",
            },
            {
                "candidate_id": "candidate-a",
                "source_ref": "session-a/turn-1",
                "content": "first synthetic fact",
                "provenance": "fixture",
                "wire_rank": 2,
                "original_position": 1,
            },
            {
                "candidate_id": "candidate-c",
                "source_ref": "session-c/turn-3",
                "content": "third synthetic fact",
                "provenance": "fixture",
                "wire_rank": 3,
                "original_position": 3,
            },
        ]

    def _built(self, *, candidates=None, **changes):
        context = self._context()
        context.update(changes)
        raw = self._candidates() if candidates is None else candidates
        snapshot = build_snapshot(raw)
        envelope = build_envelope(
            **context,
            snapshot=snapshot,
            candidates=raw,
            sequence_policy="chronological_sequence_v1",
            allow_synthetic=True,
        )
        return snapshot, envelope

    def test_envelope_is_versioned_hash_bound_and_replayable(self):
        snapshot, envelope = self._built()
        self.assertEqual(envelope["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(envelope["projection_sha256"]), 64)
        self.assertEqual(len(envelope["replay_fingerprint_sha256"]), 64)
        replay = replay_envelope(envelope, snapshot, allow_synthetic=True)
        self.assertEqual(replay["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])
        self.assertEqual(replay["final_ranks"], [1, 2])
        self.assertEqual(replay["candidate_count"], 2)

    def test_replay_rejects_candidate_count_below_delivered_count(self):
        snapshot, envelope = self._built()
        tampered = copy.deepcopy(envelope)
        tampered["membership"]["candidate_count"] = 1
        tampered["membership"]["complete"] = False
        tampered["membership"]["truncated"] = False
        base = {key: value for key, value in tampered.items()
                if key not in {"replay_fingerprint_sha256", "projection_sha256"}}
        tampered["replay_fingerprint_sha256"] = replay_module._replay_fingerprint(base)
        tampered["projection_sha256"] = replay_module.sha256_text(replay_module.stable_json({
            **base, "replay_fingerprint_sha256": tampered["replay_fingerprint_sha256"]
        }))
        with self.assertRaises(ReplayValidationError):
            replay_envelope(tampered, snapshot, allow_synthetic=True)

    def test_replay_requires_and_binds_preflight(self):
        snapshot, envelope = self._built()
        tampered = copy.deepcopy(envelope)
        tampered["preflight"]["database_identity"]["inode"] = 99
        with self.assertRaises(ReplayValidationError):
            validate_envelope(tampered)
        missing = copy.deepcopy(envelope)
        missing.pop("preflight")
        with self.assertRaises(ReplayValidationError):
            validate_envelope(missing)

    def test_preflight_rejects_unknown_fields_before_publication(self):
        context = self._context()
        preflight = copy.deepcopy(context.pop("preflight"))
        preflight["RAW-QUERY-SENTINEL"] = "must-not-cross-boundary"
        candidates = self._candidates()
        snapshot = build_snapshot(candidates)
        with self.assertRaises(ReplayValidationError):
            build_envelope(
                **context,
                preflight=preflight,
                snapshot=snapshot,
                candidates=candidates,
                sequence_policy="chronological_sequence_v1",
            )

    def test_runtime_envelope_requires_live_binding_unless_explicitly_synthetic(self):
        context = self._context()
        candidates = self._candidates()
        snapshot = build_snapshot(candidates)
        with self.assertRaises(ReplayValidationError):
            build_envelope(
                **context,
                snapshot=snapshot,
                candidates=candidates,
                sequence_policy="chronological_sequence_v1",
            )

    def test_wire_rank_and_final_placement_are_distinct(self):
        _snapshot, envelope = self._built()
        rows = envelope["candidates"]
        self.assertEqual([(row["wire_rank"], row["final_rank"]) for row in rows], [(2, 1), (1, 2)])
        self.assertEqual([row["original_position"] for row in rows], [1, 2])

    def test_reversed_wire_order_with_stale_ranks_is_rejected(self):
        self._built()
        with self.assertRaises(ReplayValidationError):
            self._built(candidates=list(reversed(self._candidates())))

    def test_truncation_is_explicit_and_membership_is_complete(self):
        _snapshot, envelope = self._built()
        self.assertEqual(envelope["membership"], {
            "candidate_count": 3,
            "delivered_count": 2,
            "requested_top_k": 2,
            "complete": True,
            "truncated": True,
        })
        self.assertEqual(envelope["status"], "complete")

    def test_incomplete_top_k_requires_partial_status(self):
        candidates = self._candidates()[:1]
        _snapshot, envelope = self._built(candidates=candidates, top_k=3, status="partial", reason="top_k_incomplete")
        self.assertEqual(envelope["membership"]["complete"], False)
        self.assertEqual(envelope["status"], "partial")
        with self.assertRaises(ReplayValidationError):
            self._built(candidates=candidates, top_k=3, status="complete")

    def test_absent_score_is_not_synthetic_zero(self):
        _snapshot, envelope = self._built()
        rows = envelope["candidates"]
        scored = next(row for row in rows if "score" in row)
        unscored = next(row for row in rows if "score" not in row)
        self.assertNotIn("score", unscored)
        self.assertNotIn("score_semantics", unscored)
        self.assertEqual(scored["score"], 0.8)
        self.assertEqual(scored["score_semantics"], "rrf-v1")

    def test_empty_and_unavailable_are_explicit(self):
        empty_snapshot, empty = self._built(candidates=[], status="empty")
        self.assertEqual(empty["status"], "empty")
        self.assertEqual(empty["membership"]["delivered_count"], 0)
        self.assertEqual(replay_envelope(empty, empty_snapshot, allow_synthetic=True)["status"], "empty")
        _snapshot, unavailable = self._built(candidates=[], status="unavailable", reason="retrieval_tool_unavailable")
        self.assertEqual(unavailable["reason"], "retrieval_tool_unavailable")
        with self.assertRaises(ReplayValidationError):
            self._built(candidates=[], status="unavailable")

    def test_malformed_rank_and_unknown_fields_fail_closed(self):
        _snapshot, envelope = self._built()
        malformed = copy.deepcopy(envelope)
        malformed["candidates"][0]["wire_rank"] = 99
        with self.assertRaises(ReplayValidationError):
            validate_envelope(malformed)
        malformed = copy.deepcopy(envelope)
        malformed["unexpected_private_field"] = "body"
        with self.assertRaises(ReplayValidationError):
            validate_envelope(malformed)

    def test_tampered_snapshot_or_projection_is_rejected(self):
        snapshot, envelope = self._built()
        tampered_snapshot = copy.deepcopy(snapshot)
        tampered_snapshot["records"][0]["content_sha256"] = "f" * 64
        with self.assertRaises(ReplayValidationError):
            replay_envelope(envelope, tampered_snapshot, allow_synthetic=True)
        tampered = copy.deepcopy(envelope)
        tampered["projection_sha256"] = "f" * 64
        with self.assertRaises(ReplayValidationError):
            validate_envelope(tampered)

    def test_replay_rejects_order_permutation_and_later_candidate_substitution(self):
        snapshot, envelope = self._built()
        permuted = copy.deepcopy(envelope)
        permuted["candidates"].reverse()
        for rank, row in enumerate(permuted["candidates"], 1):
            row["final_rank"] = rank
        replay_base = {key: value for key, value in permuted.items()
                       if key not in {"replay_fingerprint_sha256", "projection_sha256"}}
        permuted["replay_fingerprint_sha256"] = replay_module._replay_fingerprint(replay_base)
        permuted["projection_sha256"] = replay_module.sha256_text(replay_module.stable_json({
            **replay_base, "replay_fingerprint_sha256": permuted["replay_fingerprint_sha256"]
        }))
        with self.assertRaises(ReplayValidationError):
            validate_envelope(permuted)

        expected = replay_module._public_sequence_order(snapshot["records"], "chronological_sequence_v1")
        substituted = copy.deepcopy(envelope)
        substituted["candidates"] = [
            {**expected[0], "final_rank": 1},
            {**expected[2], "final_rank": 2},
        ]
        replay_base = {key: value for key, value in substituted.items()
                       if key not in {"replay_fingerprint_sha256", "projection_sha256"}}
        substituted["replay_fingerprint_sha256"] = replay_module._replay_fingerprint(replay_base)
        substituted["projection_sha256"] = replay_module.sha256_text(replay_module.stable_json({
            **replay_base, "replay_fingerprint_sha256": substituted["replay_fingerprint_sha256"]
        }))
        with self.assertRaises(ReplayValidationError):
            replay_envelope(substituted, snapshot, allow_synthetic=True)

    def test_runtime_preflight_rejects_a_forged_fresh_flag_for_stale_sqlite(self):
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
            binary.chmod(binary.stat().st_mode | 0o100)
            db = root / "run.db"
            preflight = replay_module.prepare_recall_preflight(
                binary=str(binary),
                db_path=str(db),
                dataset={"name": "fixture", "queries": []},
                config={"limit": 2},
                repo_root=str(repo_root),
            )
            db.unlink()
            connection = sqlite3.connect(str(db))
            connection.execute("CREATE TABLE stale(value TEXT)")
            connection.execute("INSERT INTO stale(value) VALUES ('old')")
            connection.commit()
            connection.close()
            identity = db.stat()
            forged = copy.deepcopy(preflight)
            forged["database_identity"] = {
                "device": identity.st_dev,
                "inode": identity.st_ino,
                "ctime_ns": identity.st_ctime_ns,
                "size": identity.st_size,
            }
            forged["database_id_sha256"] = replay_module.sha256_text(
                replay_module.stable_json(forged["database_identity"])
            )
            with self.assertRaises(ReplayValidationError):
                replay_module.validate_recall_preflight(
                    forged,
                    binary=str(binary),
                    db_path=str(db),
                    repo_root=str(repo_root),
                    dataset={"name": "fixture", "queries": []},
                    config={"limit": 2},
                )

    def test_snapshot_rejects_wire_rank_permutation_relative_to_input_order(self):
        candidates = copy.deepcopy(self._candidates()[:2])
        candidates[0]["wire_rank"] = 2
        candidates[1]["wire_rank"] = 1
        with self.assertRaises(ReplayValidationError):
            build_snapshot(candidates)

    def test_nested_recall_projection_unknown_fields_fail_closed(self):
        response = {
            "items": [{"key": "x", "body_json": {"content": "x"}}],
            "total": 1,
            "retrieval_profile": "fixture",
            "diagnostic": {
                "reason": "no_match",
                "active_memories": 0,
                "RAW-QUERY-SENTINEL": "must-not-cross",
            },
        }
        normalized = replay_module.normalize_recall_response(response, limit=1)
        self.assertEqual(normalized["status"], "unavailable")
        self.assertEqual(normalized["items"], [])

    def test_optional_wire_projections_are_not_persisted_in_public_artifacts(self):
        instance = {
            "haystack_session_ids": ["session-a"],
            "haystack_sessions": [[
                {"role": "user", "content": "PRIVATE-MEMORY-BODY"},
            ]],
            "haystack_dates": ["2023/04/20 (Thu) 00:00"],
        }
        response = {
            "items": [{
                "key": "session-a",
                "body_json": {"note": "PRIVATE-MEMORY-BODY"},
                "why_served": {
                    "reason": "PRIVATE-RAW-QUERY",
                    "memory_class": "fact",
                },
            }],
            "total": 1,
            "retrieval_profile": "fixture",
            "fused_trace": {"original_query": "PRIVATE-RAW-QUERY"},
            "evidence": {
                "status": "available",
                "items": [{"text": "PRIVATE-EVIDENCE"}],
            },
        }
        wire = replay_module.normalize_recall_response(response, limit=1)
        rows = retrieval_diag._replay_rows(instance, wire["items"])
        snapshot = build_snapshot(rows)
        context = self._context()
        context.update({"top_k": 1, "cell_id": "cell-private"})
        envelope = build_envelope(
            **context,
            snapshot=snapshot,
            candidates=rows,
            sequence_policy="wire_v1",
            status="complete",
            allow_synthetic=True,
        )
        public = replay_module.stable_json({"snapshot": snapshot, "envelope": envelope})
        for sentinel in ("PRIVATE-MEMORY-BODY", "PRIVATE-RAW-QUERY", "PRIVATE-EVIDENCE"):
            self.assertNotIn(sentinel, public)

    def test_raw_payloads_are_not_emitted(self):
        snapshot, envelope = self._built()
        public = str(envelope)
        self.assertNotIn("synthetic fact", public)
        self.assertNotIn("workspace_id", envelope)
        self.assertNotIn("scope", envelope)
        self.assertNotIn('"content":', public)
        self.assertNotIn("password", public.lower())
        self.assertEqual(snapshot["raw_inputs_captured"], False)


if __name__ == "__main__":
    unittest.main()
