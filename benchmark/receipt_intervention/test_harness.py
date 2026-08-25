"""Contract tests for receipt-conditioned evidence intervention (#1136)."""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import tempfile
import unittest

from benchmark.receipt_intervention import harness

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixture.json").read_text(encoding="utf-8"))


class ReceiptInterventionTests(unittest.TestCase):
    def build(self, fixture=None):
        return harness.build_report(copy.deepcopy(fixture or FIXTURE))

    def test_report_has_baseline_and_three_matched_intervention_arms(self):
        report = self.build()
        self.assertEqual(report["status"], "complete")
        self.assertEqual(
            [arm["arm_id"] for arm in report["arms"]],
            ["baseline", "matched-size-control", "random-control", "receipt-blocked"],
        )
        configs = {arm["arm_id"]: arm["config"] for arm in report["arms"]}
        baseline = configs["baseline"]
        for arm_id, config in configs.items():
            for field in (
                "retrieval_mode",
                "top_k",
                "context_token_budget",
                "scan_budget",
                "reader",
                "judge",
                "seed",
            ):
                self.assertEqual(config[field], baseline[field], arm_id)

    def test_receipt_is_sealed_before_intervention_and_bound_to_baseline(self):
        report = self.build()
        for case in report["cases"]:
            receipt = case["baseline_receipt"]
            self.assertTrue(receipt["sealed_before_intervention"])
            self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                receipt["receipt_sha256"],
                harness.sha256_json(
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "receipt_sha256"
                    }
                ),
            )
            self.assertTrue(
                {"blocked_source_groups", "arm_id", "intervention_sha256"}.isdisjoint(
                    receipt
                )
            )
            self.assertEqual(
                case["interventions"]["receipt-blocked"]["receipt_sha256"],
                receipt["receipt_sha256"],
            )

    def test_receipt_block_prevents_alias_lane_cache_expansion_and_fallback_reentry(
        self,
    ):
        report = self.build()
        rows = {(row["case_id"], row["arm_id"]): row for row in report["rows"]}
        for case_id in ("synonym-alternate-lane", "source-expansion", "cache-fallback"):
            row = rows[(case_id, "receipt-blocked")]
            blocked = set(row["blocked_source_groups"])
            self.assertTrue(blocked)
            self.assertTrue(blocked.isdisjoint(row["selected_source_groups"]))
            blocked_decisions = [
                item
                for item in row["candidate_decisions"]
                if item["source_group"] in blocked
            ]
            self.assertTrue(blocked_decisions)
            self.assertTrue(
                all(
                    item["disposition"] == "blocked_receipt_source_group"
                    for item in blocked_decisions
                )
            )
            self.assertGreater(len({item["lane"] for item in blocked_decisions}), 1)

    def test_controls_have_explicit_cardinality_token_and_scan_accounting(self):
        report = self.build()
        for case in report["cases"]:
            receipt = case["interventions"]["receipt-blocked"]
            random = case["interventions"]["random-control"]
            matched = case["interventions"]["matched-size-control"]
            self.assertEqual(
                random["blocked_cardinality"], receipt["blocked_cardinality"]
            )
            self.assertEqual(
                matched["blocked_cardinality"], receipt["blocked_cardinality"]
            )
            self.assertEqual(matched["blocked_tokens"], receipt["blocked_tokens"])
            for intervention in (receipt, random, matched):
                self.assertGreater(intervention["scan_budget"], 0)
                self.assertGreater(intervention["context_token_budget"], 0)
                self.assertRegex(intervention["intervention_sha256"], r"^[0-9a-f]{64}$")

    def test_report_distinguishes_receipt_gold_unreceipted_and_unavailable_evidence(
        self,
    ):
        report = self.build()
        for row in report["rows"]:
            evidence = row["evidence_accounting"]
            self.assertEqual(
                set(evidence),
                {
                    "blocked_receipt_evidence_count",
                    "blocked_gold_evidence_count",
                    "selected_unreceipted_evidence_count",
                    "unavailable_evidence_count",
                },
            )
        rows = {(row["case_id"], row["arm_id"]): row for row in report["rows"]}
        self.assertEqual(
            rows[("selected-unreceipted", "baseline")]["evidence_accounting"][
                "selected_unreceipted_evidence_count"
            ],
            1,
        )
        self.assertEqual(
            rows[("synonym-alternate-lane", "baseline")]["evidence_accounting"][
                "selected_unreceipted_evidence_count"
            ],
            0,
        )
        receipt_rows = [
            row for row in report["rows"] if row["arm_id"] == "receipt-blocked"
        ]
        self.assertTrue(
            any(
                row["evidence_accounting"]["blocked_receipt_evidence_count"] > 0
                for row in receipt_rows
            )
        )
        self.assertTrue(
            any(
                row["evidence_accounting"]["selected_unreceipted_evidence_count"] > 0
                for row in report["rows"]
            )
        )
        self.assertTrue(
            any(
                row["evidence_accounting"]["unavailable_evidence_count"] > 0
                for row in report["rows"]
            )
        )

    def test_lane_scope_as_of_duplicate_groups_and_output_alignment(self):
        report = self.build()
        for row in report["rows"]:
            self.assertEqual(row["workspace_hash"], "workspace-a")
            self.assertEqual(row["agent_id"], "agent-a")
            self.assertEqual(
                len(row["selected_source_groups"]),
                len(set(row["selected_source_groups"])),
            )
            self.assertEqual(
                row["context"]["selected_count"], len(row["selected_source_groups"])
            )
            self.assertEqual(
                row["context"]["delivered_tokens"], sum(row["selected_token_counts"])
            )
            self.assertLessEqual(
                row["context"]["delivered_tokens"], row["context"]["token_budget"]
            )
            self.assertEqual(
                row["receipt_output_alignment"],
                row["arm_id"] != "baseline" or bool(row["selected_source_groups"]),
            )

    def test_missing_malformed_stale_terminal_or_ambiguous_receipt_references_fail_closed(
        self,
    ):
        mutations = []
        missing = copy.deepcopy(FIXTURE)
        missing["cases"][0]["receipt_candidate_ids"] = ["does-not-exist"]
        mutations.append(missing)
        stale = copy.deepcopy(FIXTURE)
        stale["cases"][0]["receipt_candidate_ids"] = ["stale-alias"]
        mutations.append(stale)
        terminal = copy.deepcopy(FIXTURE)
        terminal["candidates"][0]["lifecycle"] = "tombstoned"
        mutations.append(terminal)
        wrong_scope = copy.deepcopy(FIXTURE)
        wrong_scope["cases"][0]["workspace_hash"] = "workspace-b"
        mutations.append(wrong_scope)
        ambiguous = copy.deepcopy(FIXTURE)
        ambiguous["candidates"][1]["source_ref"] = ambiguous["candidates"][0][
            "source_ref"
        ]
        ambiguous["candidates"][1]["source_group"] = "different-group"
        mutations.append(ambiguous)
        for fixture in mutations:
            with self.assertRaises(harness.ContractError):
                self.build(fixture)

    def test_report_is_deterministic_tamper_evident_and_harness_bound(self):
        first = self.build()
        self.assertEqual(
            first["commitments"]["harness_sha256"],
            hashlib.sha256((HERE / "harness.py").read_bytes()).hexdigest(),
        )
        shuffled = copy.deepcopy(FIXTURE)
        shuffled["candidates"] = list(reversed(shuffled["candidates"]))
        shuffled["cases"] = list(reversed(shuffled["cases"]))
        second = self.build(shuffled)
        self.assertEqual(first, second)
        harness.validate_report(first)
        tampered = copy.deepcopy(first)
        tampered["rows"][0]["context"]["delivered_tokens"] += 1
        with self.assertRaises(harness.ContractError):
            harness.validate_report(tampered)
        forged = copy.deepcopy(first)
        forged["commitments"]["baseline_receipt_set_sha256"] = "0" * 64
        forged_base = {
            key: value for key, value in forged.items() if key != "report_sha256"
        }
        forged["report_sha256"] = harness.sha256_json(forged_base)
        with self.assertRaises(harness.ContractError):
            harness.validate_report(forged)

    def test_provider_free_claim_boundary_and_gold_is_evaluator_only(self):
        report = self.build()
        self.assertEqual(report["execution"]["provider_calls"], 0)
        self.assertEqual(report["execution"]["answerer_calls"], 0)
        self.assertEqual(report["execution"]["judge_calls"], 0)
        self.assertFalse(report["claims"]["model_internal_causality"])
        self.assertEqual(
            report["claims"]["label"], "trace-faithfulness-evidence-necessity"
        )
        self.assertTrue(
            report["evaluator_boundary"]["gold_available_only_after_receipt_seal"]
        )
        text = json.dumps(report, sort_keys=True).lower()
        for forbidden in (
            "raw_prompt",
            "memory_body",
            "provider_payload",
            "api_key",
            "credential",
            "password",
        ):
            self.assertNotIn(forbidden, text)

    def test_committed_report_schema_and_ci_gate_match(self):
        expected = self.build()
        committed = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, expected)
        harness.validate_report(committed)
        schema = json.loads((HERE / "report.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"],
            "https://perseus.observer/schemas/receipt-intervention-v1.json",
        )
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(
            schema["properties"]["schema_version"]["const"], harness.REPORT_SCHEMA
        )
        workflow = (
            HERE.parents[1] / ".github" / "workflows" / "benchmark-quality.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("benchmark/receipt_intervention", workflow)

    def test_cli_writes_validated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "report.json"
            self.assertEqual(
                harness.main(
                    ["--fixture", str(HERE / "fixture.json"), "--out", str(output)]
                ),
                0,
            )
            harness.validate_report(json.loads(output.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
