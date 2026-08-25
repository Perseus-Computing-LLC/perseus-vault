"""Contract tests for the provider-free matched graph-context ablation (#1143)."""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import tempfile
import unittest

from benchmark.graph_context_ablation import harness

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))


class GraphContextAblationTests(unittest.TestCase):
    def build(self, fixture=None):
        return harness.build_report(copy.deepcopy(fixture or FIXTURE))

    def test_fixture_covers_required_adversarial_shapes(self):
        report = self.build()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(
            set(report["fixture_coverage"]),
            {
                "true_multi_hop",
                "single_hop_control",
                "stale_current_conflict",
                "unsupported_declared_edge",
                "cross_scope_target",
                "no_signal_utility_skip",
                "abstention",
            },
        )
        self.assertEqual(
            set(report["corpus"]["source_types"]),
            {"adr", "meeting_notes", "slack_thread", "postmortem", "service_manifest"},
        )

    def test_graph_arms_are_matched_except_for_graph_availability(self):
        report = self.build()
        cells = {cell["cell_id"]: cell for cell in report["cells"]}
        off = cells["graph-off"]
        on = cells["graph-on"]
        self.assertEqual(off["matched_config_sha256"], on["matched_config_sha256"])
        for field in (
            "retrieval_mode",
            "top_k",
            "context_token_budget",
            "reader",
            "prompt",
            "judge",
            "seed",
        ):
            self.assertEqual(off["config"][field], on["config"][field])
        self.assertFalse(off["config"]["graph_enabled"])
        self.assertTrue(on["config"]["graph_enabled"])
        self.assertEqual(report["comparison"]["intended_difference"], "graph_enabled")
        self.assertFalse(report["comparison"]["mode_comparison_included"])

    def test_graph_on_recovers_true_multihop_and_no_signal_is_observable_skip(self):
        report = self.build()
        rows = {(row["cell_id"], row["case_id"]): row for row in report["rows"]}
        off = rows[("graph-off", "true-multi-hop")]
        on = rows[("graph-on", "true-multi-hop")]
        self.assertFalse(off["retrieval_evidence"]["all_required_evidence"])
        self.assertTrue(on["retrieval_evidence"]["all_required_evidence"])
        self.assertGreater(len(on["selected_paths"]), 0)
        no_signal = rows[("graph-on", "no-signal")]
        self.assertEqual(no_signal["graph_route"]["status"], "skipped")
        self.assertEqual(no_signal["graph_route"]["reason"], "no_signal")
        self.assertTrue(no_signal["answer_metrics"]["abstained"])

    def test_selected_paths_are_supported_scoped_current_and_anchored(self):
        report = self.build()
        selected_paths = [
            path for row in report["rows"] for path in row["selected_paths"]
        ]
        self.assertTrue(selected_paths)
        for path in selected_paths:
            self.assertEqual(path["support_state"], "supported")
            self.assertTrue(path["source_id"])
            self.assertTrue(path["source_revision"])
            self.assertTrue(path["evidence_anchor"])
            self.assertRegex(path["source_digest_sha256"], r"^[0-9a-f]{64}$")
        graph_rows = [row for row in report["rows"] if row["cell_id"] == "graph-on"]
        reasons = {
            decision["reason"]
            for row in graph_rows
            for decision in row["edge_decisions"]
        }
        self.assertTrue({"unsupported_edge", "cross_scope", "stale_source"} <= reasons)
        self.assertTrue(
            all(
                row["retrieval_evidence"]["unsupported_edge_rate"] == 0.0
                for row in report["rows"]
            )
        )
        self.assertTrue(
            all(
                row["retrieval_evidence"]["stale_conflict_leakage"] == 0
                for row in report["rows"]
            )
        )

    def test_metric_classes_and_denominators_are_separate_and_complete(self):
        report = self.build()
        for cell in report["cells"]:
            metrics = cell["metrics"]
            self.assertEqual(
                set(metrics),
                {"retrieval_evidence", "answer_quality", "context_cost", "execution"},
            )
            self.assertGreater(metrics["retrieval_evidence"]["denominator"], 0)
            self.assertGreater(metrics["answer_quality"]["denominator"], 0)
            self.assertGreater(metrics["context_cost"]["token_denominator"], 0)
            self.assertEqual(metrics["execution"]["provider_calls"], 0)
            self.assertEqual(metrics["execution"]["network_calls"], 0)
            self.assertGreater(metrics["execution"]["error_denominator"], 0)
            self.assertEqual(metrics["execution"]["errors"], 0)
            self.assertEqual(metrics["execution"]["latency"]["denominator"], 0)
            self.assertEqual(
                metrics["execution"]["latency"]["status"], "not_measured_provider_free"
            )

    def test_report_is_deterministic_hash_bound_and_tamper_evident(self):
        first = self.build()
        self.assertEqual(
            first["commitments"]["harness_sha256"],
            hashlib.sha256((HERE / "harness.py").read_bytes()).hexdigest(),
        )
        second_fixture = copy.deepcopy(FIXTURE)
        second_fixture["sources"] = list(reversed(second_fixture["sources"]))
        second_fixture["edges"] = list(reversed(second_fixture["edges"]))
        second_fixture["cases"] = list(reversed(second_fixture["cases"]))
        second = self.build(second_fixture)
        self.assertEqual(first, second)
        harness.validate_report(first)
        tampered = copy.deepcopy(first)
        tampered["cells"][0]["metrics"]["retrieval_evidence"][
            "source_evidence_recall"
        ] = 0.0
        with self.assertRaises(harness.ContractError):
            harness.validate_report(tampered)

    def test_provider_free_claim_boundary_and_public_projection(self):
        report = self.build()
        self.assertEqual(report["execution"]["mode"], "provider-free-deterministic")
        self.assertEqual(report["execution"]["provider_calls"], 0)
        self.assertEqual(
            report["comparability"]["label"], "vault-owned-synthetic-diagnostic"
        )
        self.assertFalse(
            report["comparability"]["third_party_score_comparison_allowed"]
        )
        text = json.dumps(report, sort_keys=True).lower()
        for forbidden in (
            "api_key",
            "credential",
            "password",
            "provider_payload",
            "hydradb_score",
        ):
            self.assertNotIn(forbidden, text)

    def test_committed_report_matches_current_fixture(self):
        expected = self.build()
        committed = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, expected)
        harness.validate_report(committed)

    def test_schema_and_ci_gate_cover_the_report_contract(self):
        schema = json.loads((HERE / "report.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"],
            "https://perseus.observer/schemas/graph-context-ablation-v1.json",
        )
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(
            schema["properties"]["schema_version"]["const"], harness.REPORT_SCHEMA
        )
        self.assertTrue(
            {
                "cells",
                "rows",
                "commitments",
                "execution",
                "comparability",
                "report_sha256",
            }
            <= set(schema["required"])
        )
        workflow = (
            HERE.parents[1] / ".github" / "workflows" / "benchmark-quality.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("benchmark/graph_context_ablation", workflow)

    def test_cli_writes_validated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "report.json"
            rc = harness.main(
                ["--fixture", str(HERE / "fixture.json"), "--out", str(output)]
            )
            self.assertEqual(rc, 0)
            harness.validate_report(json.loads(output.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
