from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from benchmark.longmemeval.comparability import (
    COMPARABILITY_SCHEMA_VERSION,
    ManifestError,
    build_dual_lane_scorecard,
    build_manifest,
    compare_manifests,
    merge_lanes,
    read_legacy_artifact,
    validate_manifest,
)


class ComparabilityContractTests(unittest.TestCase):
    def _spec(self, lane: str = "official-compatible"):
        return {
            "manifest_id": f"fixture-{lane}",
            "lane": lane,
            "dataset": {
                "name": "longmemeval-cleaned",
                "split": "s",
                "digest_sha256": "1" * 64,
                "question_count": 10,
                "question_type_distribution": {"multi-session": 4, "temporal-reasoning": 3, "knowledge-update": 2, "single-session-preference": 1},
                "scope": "public-split-s",
                "exclusions": [],
            },
            "answerer": {
                "provider": "openai",
                "model": "gpt-4o-2024-08-06",
                "temperature": 0,
                "completion_cap": 1200,
                "retry_policy": {"max_retries": 0, "on_error": "record-and-stop"},
            },
            "judge": {
                "provider": "openai",
                "model": "gpt-4o-2024-08-06",
                "temperature": 0,
                "completion_cap": 1200,
                "retry_policy": {"max_retries": 0, "on_error": "record-and-stop"},
                "threshold": {"kind": "prefix-label", "value": 0.5, "labels": ["yes", "no"]},
            },
            "prompts": {
                "answer": {"id": "longmemeval-official-cot", "digest_sha256": "2" * 64},
                "judge": {"id": "longmemeval-official-per-type", "digest_sha256": "3" * 64},
            },
            "ingest": {
                "shape": "unique-key-per-session",
                "memory_serialization": "role-content-flattened",
                "context_representation": "ranked-full-sessions",
            },
            "retrieval": {
                "mode": "hybrid",
                "requested_depth": 10,
                "effective_depth": 10,
                "context_token_budget": 16000,
                "context_byte_budget": 64000,
                "selection_policy": "ranked-top-k",
                "assembly_policy": "full-ranked-session-order",
            },
            "evaluator": {
                "identity": "official-longmemeval-per-type-v1",
                "metric": "binary-accuracy",
                "denominator": 10,
                "excluded_cases": [],
                "failed_cases": [],
                "completion": "complete",
                "abstention": "official-absent-is-graded",
            },
            "provenance": {
                "state": "verified",
                "harness_commit": "a" * 40,
                "binary_sha256": "4" * 64,
                "run_sha256": "5" * 64,
                "custody_sha256": "6" * 64,
            },
        }

    def test_manifest_digest_is_deterministic_and_schema_versioned(self):
        first = build_manifest(self._spec())
        second_spec = copy.deepcopy(self._spec())
        second_spec["dataset"] = {key: second_spec["dataset"][key] for key in reversed(second_spec["dataset"])}
        second = build_manifest(second_spec)
        self.assertEqual(first["schema_version"], COMPARABILITY_SCHEMA_VERSION)
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        validate_manifest(first)

    def test_model_prompt_judge_and_top_k_mismatches_are_individual(self):
        reference = build_manifest(self._spec())
        candidate_spec = self._spec()
        candidate_spec["answerer"]["model"] = "gpt-5-mini"
        candidate_spec["prompts"]["answer"]["digest_sha256"] = "7" * 64
        candidate_spec["judge"]["threshold"]["value"] = 0.75
        candidate_spec["retrieval"]["effective_depth"] = 100
        candidate = build_manifest(candidate_spec)
        scorecard = compare_manifests(reference, candidate)
        mismatches = {row["field"] for row in scorecard["field_results"] if not row["match"]}
        self.assertIn("answerer.model", mismatches)
        self.assertIn("prompts.answer.digest_sha256", mismatches)
        self.assertIn("judge.threshold.value", mismatches)
        self.assertIn("retrieval.effective_depth", mismatches)
        self.assertNotIn("retrieval.requested_depth", mismatches)
        self.assertEqual(scorecard["disposition"], "not-like-for-like")
        self.assertFalse(scorecard["claimable_like_for_like"])

    def test_requested_and_effective_depth_are_distinct_fields(self):
        spec = self._spec()
        spec["retrieval"]["effective_depth"] = 100
        manifest = build_manifest(spec)
        self.assertEqual(manifest["retrieval"]["requested_depth"], 10)
        self.assertEqual(manifest["retrieval"]["effective_depth"], 100)
        self.assertNotIn("k", manifest["retrieval"])

    def test_partial_denominator_is_explicit_and_blocks_comparison(self):
        reference = build_manifest(self._spec())
        partial_spec = self._spec()
        partial_spec["evaluator"].update({"denominator": 9, "excluded_cases": ["q10"], "completion": "partial"})
        partial = build_manifest(partial_spec)
        scorecard = compare_manifests(reference, partial)
        mismatches = {row["field"] for row in scorecard["field_results"] if not row["match"]}
        self.assertIn("evaluator.denominator", mismatches)
        self.assertIn("evaluator.completion", mismatches)
        self.assertEqual(scorecard["disposition"], "not-like-for-like")

    def test_stale_unknown_missing_and_contradictory_provenance_fail_closed(self):
        for state in ("stale", "unknown"):
            spec = self._spec()
            spec["provenance"]["state"] = state
            with self.assertRaises(ManifestError):
                build_manifest(spec)
        missing = self._spec()
        del missing["prompts"]["judge"]
        with self.assertRaises(ManifestError):
            build_manifest(missing)
        malformed = self._spec()
        malformed["retrieval"]["effective_depth"] = -1
        with self.assertRaises(ManifestError):
            build_manifest(malformed)

    def test_official_and_product_lanes_cannot_be_merged(self):
        official = build_manifest(self._spec("official-compatible"))
        product_spec = self._spec("product-optimized")
        product_spec["prompts"]["answer"]["id"] = "product-task-engineered"
        product_spec["prompts"]["judge"]["id"] = "product-threshold-judge"
        product_spec["ingest"]["context_representation"] = "typed-memory-source-evidence"
        product = build_manifest(product_spec)
        dual = build_dual_lane_scorecard({"official-compatible": official, "product-optimized": product})
        self.assertFalse(dual["merged"])
        self.assertEqual(set(dual["lanes"]), {"official-compatible", "product-optimized"})
        self.assertNotIn("combined_accuracy", dual)
        with self.assertRaises(ManifestError):
            merge_lanes([official, product])

    def test_versioned_schema_is_present_and_declares_manifest_digest(self):
        schema = json.loads((Path(__file__).with_name("comparability.schema.json")).read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["schema_version"]["const"], COMPARABILITY_SCHEMA_VERSION)
        self.assertIn("manifest_sha256", schema["required"])
        self.assertIn("retrieval", schema["properties"])

    def test_existing_signed_artifact_is_readable_without_relabeling(self):
        path = Path(__file__).with_name("qa_report_cot.json")
        original = json.loads(path.read_text(encoding="utf-8"))
        inspected = read_legacy_artifact(original)
        self.assertEqual(inspected["status"], "legacy-readable")
        self.assertFalse(inspected["claimable_like_for_like"])
        self.assertNotIn("lane", original)


if __name__ == "__main__":
    unittest.main()
