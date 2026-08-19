from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from benchmark.package.common.handling_profile import (
    HANDLING_CORPUS_SCHEMA_VERSION,
    HANDLING_PROFILE_SCHEMA_VERSION,
    HandlingProfileError,
    agent_visible_case_ids,
    build_handling_profile_report,
    classify_candidate,
    redact_candidate,
    stable_json,
    sha256_text,
    validate_handling_corpus,
    validate_handling_report,
)


FIXTURE = Path(__file__).with_name("handling_profile_fixture.json")


class HandlingProfileContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_versioned_fixture_covers_profiles_outcomes_and_all_projection_fields(self):
        validate_handling_corpus(self.corpus)
        fields = {
            "content", "title", "safe_summary", "core_tags", "project", "task", "topic",
            "source_refs", "contract_id", "program_id", "workspace", "scope",
        }
        seen_profiles = {case["expected_profile"] for case in self.corpus["cases"]}
        self.assertEqual(
            seen_profiles,
            {"PUBLIC_SAFE", "INTERNAL_PROGRAM", "FCI_LIKE", "CUI_LIKE", "EXPORT_CONTROLLED_SIGNAL", "CREDENTIAL", "REVIEW_REQUIRED"},
        )
        for case in self.corpus["cases"]:
            if case["input_state"] != "malformed":
                self.assertEqual(set(case["candidate"]), fields)

    def test_report_is_deterministic_hash_only_and_zero_model_zero_network(self):
        first = build_handling_profile_report(self.corpus)
        second = build_handling_profile_report(copy.deepcopy(self.corpus))
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], HANDLING_PROFILE_SCHEMA_VERSION)
        self.assertEqual(first["provider_calls"], 0)
        self.assertEqual(first["network_calls"], 0)
        self.assertFalse(first["raw_inputs_captured"])
        self.assertEqual(first["report_sha256"], first["signature_sha256"])
        validate_handling_report(first)
        public = json.dumps(first, sort_keys=True)
        for forbidden in ("CUI-SYNTHETIC", "FCI-SYNTHETIC", "EXPORT-CONTROLLED-SYNTHETIC", "CREDENTIAL-SYNTHETIC", "Public release notes"):
            self.assertNotIn(forbidden, public)

    def test_expected_and_actual_profiles_outcomes_and_false_negative_metrics(self):
        report = build_handling_profile_report(self.corpus)
        rows = {row["case_id"]: row for row in report["cases"]}
        self.assertEqual(rows["public-safe-basic"]["actual_outcome"], "SAVE/AGENT_VISIBLE")
        self.assertEqual(rows["cui-redactable-content"]["actual_outcome"], "SAVE/AGENT_VISIBLE")
        self.assertEqual(rows["cui-summary-only"]["actual_outcome"], "PROTECTED")
        self.assertEqual(rows["credential-tag-only"]["actual_outcome"], "BLOCK")
        self.assertEqual(rows["review-ambiguous-source"]["actual_outcome"], "PENDING_REVIEW")
        self.assertEqual(rows["provider-unavailable"]["actual_outcome"], "REVIEW_REQUIRED")
        self.assertEqual(rows["malformed-metadata"]["actual_outcome"], "REVIEW_REQUIRED")
        self.assertEqual(rows["incomplete-redaction"]["actual_outcome"], "REVIEW_REQUIRED")
        self.assertEqual(report["totals"]["false_negative_count"], 0)
        self.assertEqual(report["totals"]["false_positive_count"], 0)
        self.assertEqual(report["totals"]["mismatch_count"], 0)
        self.assertGreater(report["totals"]["missingness_count"], 0)
        self.assertEqual(report["outcome_counts"]["BLOCK"], 2)
        self.assertEqual(report["outcome_counts"]["PROTECTED"], 6)
        self.assertEqual(report["outcome_counts"]["PENDING_REVIEW"], 1)

    def test_single_field_classifier_and_redaction_reclassification(self):
        cases = {case["case_id"]: case for case in self.corpus["cases"]}
        self.assertEqual(classify_candidate(cases["fci-title-only"]["candidate"])["profile"], "FCI_LIKE")
        self.assertEqual(classify_candidate(cases["cui-summary-only"]["candidate"])["profile"], "CUI_LIKE")
        self.assertEqual(classify_candidate(cases["export-project-only"]["candidate"])["profile"], "EXPORT_CONTROLLED_SIGNAL")
        self.assertEqual(classify_candidate(cases["credential-tag-only"]["candidate"])["profile"], "CREDENTIAL")
        self.assertEqual(classify_candidate(cases["internal-topic-only"]["candidate"])["profile"], "INTERNAL_PROGRAM")
        candidate = cases["cui-redactable-content"]["candidate"]
        redacted, complete = redact_candidate(candidate)
        self.assertTrue(complete)
        self.assertNotIn("CUI-SYNTHETIC", stable_json(redacted))
        self.assertEqual(classify_candidate(redacted)["profile"], "PUBLIC_SAFE")
        incomplete, complete = redact_candidate(candidate, exclude_fields=("content",))
        self.assertFalse(complete)
        self.assertIn("CUI-SYNTHETIC", stable_json(incomplete))

    def test_scope_isolation_hides_protected_originals_from_agent_projection(self):
        report = build_handling_profile_report(self.corpus)
        visible_a = agent_visible_case_ids(report, "workspace-a")
        visible_b = agent_visible_case_ids(report, "workspace-b")
        self.assertIn("public-safe-basic", visible_a)
        self.assertIn("cui-redactable-content", visible_a)
        self.assertNotIn("cui-summary-only", visible_a)
        self.assertNotIn("credential-tag-only", visible_a)
        self.assertNotIn("export-project-only", visible_a)
        self.assertEqual(visible_b, [])
        self.assertEqual(report["scope_isolation"]["protected_recall_exposure_count"], 0)
        self.assertEqual(report["scope_isolation"]["redacted_original_exposure_count"], 0)

    def test_tamper_unknown_fields_and_invalid_policy_fail_closed(self):
        report = build_handling_profile_report(self.corpus)
        forged = copy.deepcopy(report)
        forged["cases"][0]["actual_outcome"] = "PROTECTED"
        forged["cases"][0]["decision_state"] = "PROTECTED"
        base = {key: value for key, value in forged.items() if key not in {"report_sha256", "signature_sha256"}}
        forged["report_sha256"] = sha256_text(stable_json(base))
        forged["signature_sha256"] = forged["report_sha256"]
        with self.assertRaises(HandlingProfileError):
            validate_handling_report(forged)
        unknown = copy.deepcopy(report)
        unknown["cases"][0]["raw_content"] = "must not be accepted"
        with self.assertRaises(HandlingProfileError):
            validate_handling_report(unknown)
        malformed = copy.deepcopy(self.corpus)
        malformed["cases"][0]["expected_outcome"] = "DROP"
        with self.assertRaises(HandlingProfileError):
            build_handling_profile_report(malformed)

    def test_schemas_are_versioned(self):
        corpus_schema = json.loads(FIXTURE.with_name("handling_profile_corpus.schema.json").read_text(encoding="utf-8"))
        report_schema = json.loads(FIXTURE.with_name("handling_profile_report.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(corpus_schema["properties"]["schema_version"]["const"], HANDLING_CORPUS_SCHEMA_VERSION)
        self.assertEqual(report_schema["properties"]["schema_version"]["const"], HANDLING_PROFILE_SCHEMA_VERSION)
        self.assertIn("report_sha256", report_schema["required"])


if __name__ == "__main__":
    unittest.main()
