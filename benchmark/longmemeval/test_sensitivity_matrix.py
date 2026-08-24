"""Provider-free LongMemEval reader/judge sensitivity matrix tests."""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sensitivity_matrix as matrix  # noqa: E402


FIXTURE_PATH = HERE / "sensitivity_matrix_fixture.json"
SCHEMA_PATH = HERE / "sensitivity_matrix.schema.json"


class SensitivityMatrixContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def build(self, fixture=None):
        return matrix._s1138_build_report(copy.deepcopy(fixture or self.fixture))

    def test_fixture_builds_complete_provider_free_report(self):
        report = self.build()
        matrix._s1138_validate_report(report)
        self.assertEqual(report["schema_version"], matrix._s1138_SCHEMA_VERSION)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["execution"]["network_calls"], 0)
        self.assertEqual(report["execution"]["provider_calls"], 0)
        self.assertFalse(report["execution"]["raw_provider_payloads_captured"])
        self.assertEqual(len(report["cells"]), 5)

    def test_reader_judge_prompt_and_cutoff_swaps_are_distinct_cells(self):
        report = self.build()
        cells = {cell["cell_id"]: cell for cell in report["cells"]}
        self.assertEqual(len({cell["cell_config_sha256"] for cell in cells.values()}), 5)
        self.assertNotEqual(
            cells["cell-reader-swap"]["reader"]["model"],
            cells["cell-official-correlated"]["reader"]["model"],
        )
        self.assertNotEqual(
            cells["cell-judge-swap"]["judge"]["model"],
            cells["cell-official-correlated"]["judge"]["model"],
        )
        self.assertNotEqual(
            cells["cell-prompt-swap"]["prompt"]["lane"],
            cells["cell-official-correlated"]["prompt"]["lane"],
        )
        self.assertNotEqual(
            cells["cell-cutoff-swap"]["retrieval"]["requested_depth"],
            cells["cell-official-correlated"]["retrieval"]["requested_depth"],
        )
        self.assertNotEqual(
            cells["cell-cutoff-swap"]["retrieval"]["context_token_budget"],
            cells["cell-official-correlated"]["retrieval"]["context_token_budget"],
        )
        self.assertEqual(report["baseline_cell_id"], "cell-official-correlated")

    def test_same_model_answerer_judge_is_explicitly_correlated(self):
        report = self.build()
        baseline = next(cell for cell in report["cells"] if cell["cell_id"] == report["baseline_cell_id"])
        self.assertEqual(baseline["reader"]["model"], baseline["judge"]["model"])
        self.assertEqual(baseline["judge"]["relation"], "correlated-same-model")
        self.assertFalse(baseline["judge"]["independence_claim_eligible"])
        self.assertNotEqual(baseline["judge"]["relation"], "independent-validation")
        audit = report["validation"]["same_model_audit"]
        self.assertEqual(audit["correlated_cell_ids"], ["cell-cutoff-swap", "cell-official-correlated", "cell-prompt-swap"])
        self.assertFalse(audit["independent_validation_claim_allowed"])

    def test_report_separates_retrieval_qa_negative_judge_and_telemetry(self):
        report = self.build()
        for cell in report["cells"]:
            self.assertIn("retrieval", cell["metrics"])
            self.assertIn("qa", cell["metrics"])
            self.assertIn("abstention", cell["metrics"])
            self.assertIn("judge", cell["metrics"])
            self.assertIn("telemetry", cell["metrics"])
            self.assertIn("latency_ms", cell["metrics"]["telemetry"])
            self.assertIn("calls", cell["metrics"]["telemetry"])
            self.assertIn("provider_usage", cell["metrics"]["telemetry"])
            self.assertIn("provider_cost_microusd", cell["metrics"]["telemetry"]["provider_usage"])
        self.assertNotEqual(
            report["cells"][0]["metrics"]["retrieval"],
            report["cells"][0]["metrics"]["qa"],
        )

    def test_paired_rows_and_category_deltas_are_hash_only(self):
        report = self.build()
        self.assertEqual(len(report["paired_rows"]), 2)
        for row in report["paired_rows"]:
            self.assertRegex(row["question_id_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("question_id", row)
            self.assertEqual(
                {item["cell_id"] for item in row["cells"]},
                set(report["required_cell_ids"]),
            )
            for item in row["cells"]:
                self.assertNotIn("answer", item)
                self.assertNotIn("response", item)
                self.assertIn("answer_verdict", item)
        delta_cells = {row["cell_id"] for row in report["category_deltas"]}
        self.assertEqual(delta_cells, set(report["required_cell_ids"]))
        self.assertTrue(any(row["cell_id"] != report["baseline_cell_id"] for row in report["category_deltas"]))
        self.assertTrue(report["sensitivity_table"])

    def test_report_is_not_aliased_to_input_and_baseline_is_not_post_processed(self):
        fixture = copy.deepcopy(self.fixture)
        report = self.build(fixture)
        before = json.dumps(report, sort_keys=True)
        fixture["cells"][1]["outcomes"][0]["answer_verdict"] = "incorrect"
        fixture["cells"][1]["reader"]["model"] = "mutated-after-build"
        self.assertEqual(before, json.dumps(report, sort_keys=True))
        self.assertNotEqual(
            report["cells"][0]["outcomes_sha256"],
            report["cells"][1]["outcomes_sha256"],
        )
        self.assertNotIn("baseline-post-processed", json.dumps(report, sort_keys=True))

    def test_build_is_byte_deterministic_under_input_order_changes(self):
        fixture = copy.deepcopy(self.fixture)
        first = self.build(fixture)
        fixture["cells"] = list(reversed(fixture["cells"]))
        for cell in fixture["cells"]:
            cell["outcomes"] = list(reversed(cell["outcomes"]))
        second = self.build(fixture)
        self.assertEqual(first, second)
        self.assertEqual(
            first["report_sha256"],
            matrix._s1138_sha256_text(matrix._s1138_stable_json({
                key: value for key, value in first.items() if key != "report_sha256"
            })),
        )

    def test_missing_protocol_fields_fail_closed(self):
        cases = []
        missing_judge_digest = copy.deepcopy(self.fixture)
        del missing_judge_digest["cells"][0]["judge"]["prompt_digest_sha256"]
        cases.append(missing_judge_digest)
        missing_effective_depth = copy.deepcopy(self.fixture)
        del missing_effective_depth["cells"][0]["retrieval"]["effective_depth"]
        cases.append(missing_effective_depth)
        missing_usage = copy.deepcopy(self.fixture)
        del missing_usage["cells"][0]["outcomes"][0]["usage"]
        cases.append(missing_usage)
        missing_cell = copy.deepcopy(self.fixture)
        missing_cell["cells"] = missing_cell["cells"][:-1]
        cases.append(missing_cell)
        for bad in cases:
            with self.assertRaises(matrix._s1138_SensitivityValidationError):
                self.build(bad)

    def test_correlated_relation_and_raw_payload_markers_fail_closed(self):
        bad_relation = copy.deepcopy(self.fixture)
        bad_relation["cells"][0]["judge"]["relation"] = "independent-validation"
        with self.assertRaises(matrix._s1138_SensitivityValidationError):
            self.build(bad_relation)

        raw_payload = copy.deepcopy(self.fixture)
        raw_payload["cells"][0]["outcomes"][0]["response"] = "raw response must not be accepted"
        with self.assertRaises(matrix._s1138_SensitivityValidationError):
            self.build(raw_payload)

    def test_provider_free_fixture_rejects_nonzero_calls_or_usage(self):
        bad_calls = copy.deepcopy(self.fixture)
        bad_calls["cells"][0]["outcomes"][0]["calls"]["answerer"] = 1
        with self.assertRaises(matrix._s1138_SensitivityValidationError):
            self.build(bad_calls)

        bad_usage = copy.deepcopy(self.fixture)
        bad_usage["cells"][0]["outcomes"][0]["usage"]["answer_prompt_tokens"] = 10
        with self.assertRaises(matrix._s1138_SensitivityValidationError):
            self.build(bad_usage)

    def test_tampered_derived_metrics_fail_even_with_recomputed_report_digest(self):
        report = self.build()
        report["cells"][0]["metrics"]["qa"]["accuracy"] = 0.0
        report_base = {key: value for key, value in report.items() if key != "report_sha256"}
        report["report_sha256"] = matrix._s1138_sha256_text(matrix._s1138_stable_json(report_base))
        with self.assertRaises(matrix._s1138_SensitivityValidationError):
            matrix._s1138_validate_report(report)

    def test_provider_free_cli_writes_a_validated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = pathlib.Path(tmp) / "matrix-report.json"
            argv = [
                "sensitivity_matrix.py", "--fixture", str(FIXTURE_PATH),
                "--out", str(output),
            ]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(matrix._s1138_main(), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            matrix._s1138_validate_report(report)
            self.assertEqual(report["execution"]["provider_calls"], 0)

    def test_schema_is_versioned_strict_and_covers_required_axes(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "https://perseus.observer/schemas/longmemeval-sensitivity-matrix-v1.json")
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["$defs"]["metrics"]["additionalProperties"], False)
        required = set(schema["required"])
        self.assertTrue({
            "schema_version", "dataset", "baseline_cell_id", "required_cell_ids",
            "cells", "paired_rows", "category_deltas", "commitments", "execution",
        } <= required)
        cell_required = set(schema["$defs"]["cell"]["required"])
        self.assertTrue({
            "cell_id", "reader", "judge", "prompt", "retrieval", "retry_policy",
            "denominator_policy", "artifact_commitments", "outcomes",
        } <= cell_required)

    def test_matrix_module_has_no_provider_execution_surface(self):
        source = (HERE / "sensitivity_matrix.py").read_text(encoding="utf-8")
        for marker in ("call_llm", "OPENAI_API_KEY", "urllib.request", "perseus_vault_recall", "perseus_vault_remember"):
            self.assertNotIn(marker, source)

    def test_public_projection_has_no_raw_prompts_memories_or_provider_payloads(self):
        report_text = json.dumps(self.build(), sort_keys=True)
        for marker in (
            "raw_prompt", "memory_body", "answer_text", "response_text",
            "judge_raw", "api_key", "credential", "password",
        ):
            self.assertNotIn(marker, report_text.lower())


if __name__ == "__main__":
    unittest.main()
