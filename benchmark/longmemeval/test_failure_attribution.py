# Provider-free failure-attribution contract tests for LongMemEval #1132.
from __future__ import annotations

import copy
import inspect
import json
import pathlib
import sys
import unittest


HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = HERE / "fixtures" / "failure_attribution_fixture.json"
sys.path.insert(0, str(HERE))

import failure_attribution as gate  # noqa: E402


class FailureAttributionContractTests(unittest.TestCase):
    def test_synthetic_fixture_covers_required_semantic_scenarios(self):
        cases = gate.load_synthetic_fixture(FIXTURE)
        self.assertEqual(len(cases), 6)
        self.assertEqual(
            {case["scenario"] for case in cases},
            {
                "preference_authority",
                "multi_session_composition",
                "latest_version_selection",
                "temporal_anchor",
                "assembly_loss",
                "answer_synthesis",
            },
        )

    def test_synthetic_fixture_produces_each_bounded_attribution_class(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        cases = {row["question_id"]: row for row in report["cases"]}
        expected = {
            "fixture-pref": "preference_provenance_mishandled",
            "fixture-multi": "required_evidence_absent_from_selected_context",
            "fixture-version": "version_semantics_mishandled",
            "fixture-temporal": "temporal_semantics_mishandled",
            "fixture-assembly": "required_evidence_selected_but_poorly_assembled",
            "fixture-synthesis": "answer_synthesis_failed",
        }
        for question_id, primary_reason in expected.items():
            self.assertEqual(cases[question_id]["primary_reason"], primary_reason)
        self.assertTrue(cases["fixture-pref"]["user_evidence_present"])
        self.assertTrue(cases["fixture-version"]["latest_version_selected"])
        self.assertTrue(cases["fixture-temporal"]["temporal_anchor_present"])
        self.assertFalse(cases["fixture-assembly"]["source_token_preserved"])

    def test_public_report_has_no_raw_payload_surface(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        encoded = json.dumps(report, sort_keys=True)
        for forbidden in (
            "I prefer tea",
            "The assistant suggests coffee",
            "secret fixture body",
            "\"question\"",
            "\"answer\"",
            "\"content\"",
            "\"turns\"",
            "\"sessions\"",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertTrue(report["offline"])
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(report["judge_calls"], 0)
        self.assertFalse(report["raw_inputs_captured"])
        gate.validate_report(report)

    def test_report_digest_is_deterministic_and_tamper_evident(self):
        first = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        second = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        self.assertEqual(first, second)
        tampered = json.loads(json.dumps(first))
        tampered["cases"][0]["selected_required_count"] += 1
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(tampered)

    def test_validator_recomputes_derived_summary_before_accepting_signature(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        forged = copy.deepcopy(report)
        forged["summary"]["n"] += 100
        self._resign(forged)
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(forged)

        forged_nested = copy.deepcopy(report)
        forged_nested["provider_free_attribution"]["summary"]["n"] += 100
        self._resign(forged_nested)
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(forged_nested)

    @staticmethod
    def _resign(report):
        base = {key: value for key, value in report.items() if key not in {"projection_sha256", "signature_sha256"}}
        projection = gate.sha256_json(base)
        report["projection_sha256"] = projection
        report["signature_sha256"] = gate.sha256_json({**base, "projection_sha256": projection})

    def test_public_report_rejects_resigned_unknown_nested_fields(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        report["provider_free_attribution"]["unexpected"] = True
        self._resign(report)
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(report)

    def test_malformed_public_values_fail_closed_with_attribution_error(self):
        mutations = (
            lambda report: report.__setitem__("metric_classes", [{}]),
            lambda report: report["cases"][0].__setitem__("question_id", {}),
            lambda report: report["cases"][0].__setitem__("reason_codes", [{}]),
            lambda report: report.__setitem__("claim_boundary", {"raw": "value"}),
        )
        for mutate in mutations:
            report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
            mutate(report)
            self._resign(report)
            with self.assertRaises(gate.AttributionError):
                gate.validate_report(report)

    def test_judged_reference_rejects_nested_non_public_types(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        report["judged_qa_reference"] = {
            "metric_class": "judged_qa_reference",
            "source_status": "accepted_with_correction",
            "answerer_model": {"raw": "prompt text"},
            "judge_model": "judge",
            "systems": {
                system: {"n": 1, "correct": 1, "accuracy": 1.0}
                for system in ("fullcontext", "oracle", "perseus-vault", "stateless")
            },
            "claim": "historical",
        }
        self._resign(report)
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(report)

        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        report["judged_qa_reference"] = {
            "metric_class": "judged_qa_reference",
            "source_status": "accepted_with_correction",
            "answerer_model": "answerer",
            "judge_model": "judge",
            "systems": {
                "fullcontext": {"n": "one", "correct": 1, "accuracy": 1.0},
                "oracle": {"n": 1, "correct": 1, "accuracy": 1.0},
                "perseus-vault": {"n": 1, "correct": 1, "accuracy": 1.0},
                "stateless": {"n": 1, "correct": 1, "accuracy": 1.0},
            },
            "claim": "historical",
        }
        self._resign(report)
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(report)

    def test_unknown_fixture_fields_fail_closed(self):
        cases = gate.load_synthetic_fixture(FIXTURE)
        malformed = json.loads(json.dumps(cases))
        malformed[0]["unexpected"] = True
        with self.assertRaises(gate.AttributionError):
            gate.validate_fixture(malformed)

    def test_public_report_rejects_unknown_fields(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        report["unexpected"] = True
        with self.assertRaises(gate.AttributionError):
            gate.validate_report(report)

    def test_real_gate_module_is_provider_free_and_does_not_change_qa_path(self):
        source = inspect.getsource(gate)
        self.assertNotIn("openai", source.lower())
        self.assertNotIn("urllib", source.lower())
        self.assertIn("\"provider_calls\": 0", source)
        self.assertIn("\"judge_calls\": 0", source)

    def test_public_schema_declares_exact_case_fields(self):
        schema = json.loads((HERE / "failure_attribution.schema.json").read_text(encoding="utf-8"))
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        self.assertEqual(set(report) - set(schema["properties"]), set())
        case_schema = schema["$defs"]["case"]
        self.assertEqual(set(report["cases"][0]), set(case_schema["properties"]))
        self.assertTrue(schema["properties"]["offline"]["const"])
        self.assertEqual(schema["properties"]["provider_calls"]["const"], 0)

    def test_schema_closes_nested_projection_shapes(self):
        schema = json.loads((HERE / "failure_attribution.schema.json").read_text(encoding="utf-8"))
        judged_systems = schema["$defs"]["judged_qa_reference"]["properties"]["systems"]
        self.assertFalse(judged_systems["additionalProperties"])
        self.assertEqual(
            set(judged_systems["properties"]),
            {"fullcontext", "oracle", "perseus-vault", "stateless"},
        )
        self.assertEqual(
            schema["properties"]["selection_recovery"]["$ref"],
            "#/$defs/selection_recovery",
        )
        unavailable = schema["$defs"]["selected_slice_reference"]["oneOf"][0]
        self.assertEqual(unavailable["required"], ["available"])

    def test_report_preserves_metric_class_boundaries(self):
        report = gate.build_fixture_report(gate.load_synthetic_fixture(FIXTURE))
        self.assertEqual(
            set(report["metric_classes"]),
            {"provider_free_attribution", "synthetic_fixture_reference"},
        )
        self.assertNotIn("judged_qa_accuracy", report["metric_classes"])
        self.assertFalse(report["candidate_gate"]["paid_canary_authorized"])
        self.assertFalse(report["candidate_gate"]["paid_canary_started"])

    def test_hash_only_jsonl_replay_normalizes_to_dataset_session_ids(self):
        import tempfile
        from benchmark.package.common.replay import build_envelope, build_snapshot, sha256_text

        cases = gate.load_synthetic_fixture(FIXTURE)
        dataset = [dict(case, answer_session_ids=list(case["required_evidence_ids"]), haystack_session_ids=[session["session_id"] for session in case["sessions"]] + ([case["sessions"][0]["session_id"]] if case["question_id"] == "fixture-pref" else []), haystack_dates=[session["date"] for session in case["sessions"]], haystack_sessions=[session["turns"] for session in case["sessions"]]) for case in cases]
        lines = []
        for case in dataset:
            raw = []
            for position, session_id in enumerate(case["ranked_ids"], 1):
                identity = sha256_text(session_id)
                raw.append(
                    {
                        "candidate_id": f"candidate-{identity}",
                        "source_ref": f"source-{identity}",
                        "content": f"opaque-{position}",
                        "provenance": "vault-recall",
                        "wire_rank": position,
                        "original_position": position,
                    }
                )
            snapshot = build_snapshot(raw)
            envelope = build_envelope(
                workspace_id="test-workspace",
                scope="question:" + case["question_id"],
                fixture_id="longmemeval-retrieval-v1",
                corpus_sha256=sha256_text("corpus"),
                retrieval_profile="longmemeval-hybrid-v1",
                mode="hybrid",
                top_k=len(raw),
                cell_id=case["question_id"],
                request_sha256=sha256_text(case["question_id"]),
                config_sha256=sha256_text("config"),
                code_sha256=sha256_text("code"),
                context_policy="test-policy",
                context_policy_version="1",
                snapshot=snapshot,
                candidates=raw,
            )
            lines.append(json.dumps(envelope, sort_keys=True))

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "replay.jsonl"
            path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
            normalized = gate.load_retrieval_replay(
                path, dataset, expected_cases=len(dataset), expected_depth=1
            )

        rows = {row["question_id"]: row for row in normalized["per_question"]}
        self.assertEqual(rows["fixture-pref"]["modes"]["hybrid"]["top"], ["pref-session", "pref-distractor"])
        self.assertEqual(rows["fixture-pref"]["evidence"], ["pref-session"])
        self.assertEqual(normalized["n_instances"], len(dataset))


if __name__ == "__main__":
    unittest.main()
