import copy
import unittest

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
        )
        return snapshot, envelope

    def test_envelope_is_versioned_hash_bound_and_replayable(self):
        snapshot, envelope = self._built()
        self.assertEqual(envelope["schema_version"], SCHEMA_VERSION)
        self.assertEqual(len(envelope["projection_sha256"]), 64)
        self.assertEqual(len(envelope["replay_fingerprint_sha256"]), 64)
        replay = replay_envelope(envelope, snapshot)
        self.assertEqual(replay["replay_fingerprint_sha256"], envelope["replay_fingerprint_sha256"])
        self.assertEqual(replay["final_ranks"], [1, 2])
        self.assertEqual(replay["candidate_count"], 2)

    def test_wire_rank_and_final_placement_are_distinct(self):
        _snapshot, envelope = self._built()
        rows = envelope["candidates"]
        self.assertEqual([(row["wire_rank"], row["final_rank"]) for row in rows], [(2, 1), (1, 2)])
        self.assertEqual([row["original_position"] for row in rows], [1, 2])

    def test_repeated_builds_are_byte_stable_and_input_order_independent(self):
        first_snapshot, first = self._built()
        second_snapshot, second = self._built(candidates=list(reversed(self._candidates())))
        self.assertEqual(first_snapshot, second_snapshot)
        self.assertEqual(first, second)
        validate_envelope(first)

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
        self.assertEqual(replay_envelope(empty, empty_snapshot)["status"], "empty")
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
            replay_envelope(envelope, tampered_snapshot)
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
            replay_envelope(substituted, snapshot)

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
