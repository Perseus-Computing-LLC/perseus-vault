"""Contract tests for the edge resource-envelope benchmark (#1105)."""

from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import sys
import unittest

from benchmark.resource_envelope import collector, harness

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
OBSERVATIONS = json.loads(
    (HERE / "fixture_observations.json").read_text(encoding="utf-8")
)


class ResourceEnvelopeTests(unittest.TestCase):
    def build(self, manifest=None, observations=None):
        return harness.build_report(
            copy.deepcopy(manifest or MANIFEST),
            copy.deepcopy(observations or OBSERVATIONS),
        )

    def test_manifest_pins_profiles_corpora_budgets_phases_and_identity(self):
        normalized = harness.validate_manifest(copy.deepcopy(MANIFEST))
        self.assertEqual(normalized["schema_version"], harness.MANIFEST_SCHEMA)
        self.assertGreaterEqual(len(normalized["profiles"]), 2)
        self.assertGreaterEqual(len(normalized["corpora"]), 2)
        self.assertGreaterEqual(len(normalized["budgets"]), 2)
        self.assertEqual(
            {case["phase"] for case in normalized["cases"]}, {"cold", "warm"}
        )
        self.assertRegex(normalized["vault_revision"], r"^[0-9a-f]{40}$")
        for profile in normalized["profiles"]:
            self.assertRegex(profile["profile_digest"], r"^[0-9a-f]{64}$")
            self.assertIn("hardware_class", profile)
            self.assertIn("deployment_profile", profile)
            self.assertIn("backend_runtime_manifest", profile)

    def test_manifest_rejects_unknown_duplicate_or_unbound_dimensions(self):
        mutations = []
        unknown = copy.deepcopy(MANIFEST)
        unknown["unexpected"] = True
        mutations.append(unknown)
        duplicate = copy.deepcopy(MANIFEST)
        duplicate["profiles"].append(copy.deepcopy(duplicate["profiles"][0]))
        mutations.append(duplicate)
        unbound = copy.deepcopy(MANIFEST)
        unbound["cases"][0]["profile_id"] = "missing-profile"
        mutations.append(unbound)
        bad_digest = copy.deepcopy(MANIFEST)
        bad_digest["profiles"][0]["profile_digest"] = "0" * 64
        mutations.append(bad_digest)
        for manifest in mutations:
            with self.assertRaises(harness.ContractError):
                harness.validate_manifest(manifest)

    def test_observed_outcome_may_vary_within_frozen_expected_set(self):
        manifest = copy.deepcopy(MANIFEST)
        target = next(
            case for case in manifest["cases"] if case["id"] == "available-warm"
        )
        target["expected_outcomes"] = ["available", "partial"]
        observations = copy.deepcopy(OBSERVATIONS)
        for observation in observations:
            if observation["case_id"] == target["id"]:
                observation["outcome_status"] = "partial"
        report = self.build(manifest=manifest, observations=observations)
        statuses = {
            sample["outcome_status"]
            for sample in report["samples"]
            if sample["case_id"] == target["id"]
        }
        self.assertEqual(statuses, {"partial"})

    def test_report_covers_available_empty_partial_unavailable_timeout_and_degraded(
        self,
    ):
        report = self.build()
        statuses = {sample["outcome_status"] for sample in report["samples"]}
        self.assertEqual(
            statuses,
            {"available", "empty", "partial", "unavailable", "timeout", "degraded"},
        )
        self.assertEqual(report["execution"]["network_calls"], 0)
        self.assertTrue(report["execution"]["offline"])

    def test_resource_and_workload_observations_are_explicit(self):
        report = self.build()
        required_resources = {
            "wall_time_ns",
            "cpu_time_ns",
            "peak_rss_bytes",
            "read_bytes",
            "write_bytes",
            "power_watts",
            "energy_joules",
        }
        required_workload = {
            "measurement_overhead_ns",
            "net_wall_time_ns",
            "recall_candidate_count",
            "selected_count",
            "traversal_depth",
            "context_assembly_ns",
            "output_bytes",
            "output_tokens",
        }
        for sample in report["samples"]:
            self.assertEqual(set(sample["resources"]), required_resources)
            self.assertEqual(set(sample["workload"]), required_workload)
            for field in ("power_watts", "energy_joules"):
                self.assertEqual(sample["resources"][field]["status"], "unavailable")
                self.assertNotIn("value", sample["resources"][field])

    def test_measurement_overhead_is_separate_and_net_wall_is_recomputed(self):
        report = self.build()
        available = next(
            sample
            for sample in report["samples"]
            if sample["outcome_status"] == "available"
        )
        wall = available["resources"]["wall_time_ns"]["value"]
        overhead = available["workload"]["measurement_overhead_ns"]["value"]
        net = available["workload"]["net_wall_time_ns"]["value"]
        self.assertEqual(net, max(0, wall - overhead))
        forged = copy.deepcopy(OBSERVATIONS)
        forged[0]["workload"]["net_wall_time_ns"] = {"status": "available", "value": 1}
        with self.assertRaises(harness.ContractError):
            self.build(observations=forged)

    def test_aggregates_separate_dimensions_and_report_repeatability_tolerance(self):
        report = self.build()
        dimensions = report["aggregates"]["dimensions"]
        self.assertEqual(
            set(dimensions),
            {"profile", "corpus", "budget", "phase", "outcome_status"},
        )
        self.assertIn(
            report["reproducibility"]["status"],
            {"within_tolerance", "outside_tolerance"},
        )
        self.assertGreater(report["reproducibility"]["evaluated_groups"], 0)
        for group in report["reproducibility"]["groups"]:
            self.assertIn("relative_spread", group)
            self.assertIn("tolerance", group)
            self.assertIn(group["status"], {"within_tolerance", "outside_tolerance"})

    def test_public_report_is_sanitized_and_makes_no_low_swap_claim(self):
        report = self.build()
        self.assertFalse(report["claims"]["low_swap_established"])
        self.assertFalse(report["claims"]["partner_hardware_benchmarked"])
        self.assertEqual(report["claims"]["label"], "resource-envelope-observation")
        text = json.dumps(report, sort_keys=True).lower()
        for forbidden in (
            "raw_prompt",
            "query_text",
            "memory_body",
            "body_json",
            "authorization",
            "api_key",
            "credential",
            "password",
        ):
            self.assertNotIn(forbidden, text)

    def test_report_is_deterministic_hash_bound_and_tamper_evident(self):
        first = self.build()
        second = self.build(
            manifest={
                **copy.deepcopy(MANIFEST),
                "cases": list(reversed(MANIFEST["cases"])),
            },
            observations=list(reversed(copy.deepcopy(OBSERVATIONS))),
        )
        self.assertEqual(first, second)
        harness.validate_report(first)
        self.assertEqual(
            first["commitments"]["harness_sha256"],
            hashlib.sha256((HERE / "harness.py").read_bytes()).hexdigest(),
        )
        tampered = copy.deepcopy(first)
        tampered["samples"][0]["resources"]["wall_time_ns"]["value"] += 1
        unsigned = {
            key: value for key, value in tampered.items() if key != "report_sha256"
        }
        tampered["report_sha256"] = harness.sha256_json(unsigned)
        with self.assertRaises(harness.ContractError):
            harness.validate_report(tampered)

    def test_collector_bounds_driver_timeout_and_drops_raw_fields(self):
        case = copy.deepcopy(MANIFEST["cases"][0])
        case["scenario"] = "timeout"
        result = collector.collect_case(
            [sys.executable, str(HERE / "fixture_driver.py")],
            case,
            repetition=0,
            timeout_seconds=0.05,
        )
        self.assertEqual(result["outcome_status"], "timeout")
        self.assertNotIn("raw_prompt", json.dumps(result).lower())
        self.assertEqual(result["resources"]["power_watts"]["status"], "unavailable")

    def test_collector_captures_fixture_driver_metrics_without_network(self):
        case = copy.deepcopy(MANIFEST["cases"][0])
        case["scenario"] = "available"
        result = collector.collect_case(
            [sys.executable, str(HERE / "fixture_driver.py")],
            case,
            repetition=0,
            timeout_seconds=2.0,
        )
        self.assertEqual(result["outcome_status"], "available")
        self.assertEqual(result["workload"]["selected_count"]["value"], 3)
        self.assertEqual(result["workload"]["recall_candidate_count"]["value"], 7)
        self.assertGreater(result["resources"]["wall_time_ns"]["value"], 0)
        self.assertEqual(result["network_calls"], 0)

    def test_collector_reports_cpu_unavailable_without_unix_resource_api(self):
        case = copy.deepcopy(MANIFEST["cases"][0])
        case["scenario"] = "available"
        original = collector._resource
        collector._resource = None
        try:
            result = collector.collect_case(
                [sys.executable, str(HERE / "fixture_driver.py")],
                case,
                repetition=0,
                timeout_seconds=2.0,
            )
        finally:
            collector._resource = original
        self.assertEqual(result["resources"]["cpu_time_ns"]["status"], "unavailable")

    def test_committed_report_schema_ci_and_vault_driver_contract(self):
        expected = self.build()
        committed = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, expected)
        schema = json.loads((HERE / "report.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            schema["$id"], "https://perseus.observer/schemas/resource-envelope-v1.json"
        )
        self.assertFalse(schema["additionalProperties"])
        workflow = (
            HERE.parents[1] / ".github" / "workflows" / "benchmark-quality.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("benchmark/resource_envelope", workflow)
        driver_text = (HERE / "vault_driver.py").read_text(encoding="utf-8")
        self.assertIn("perseus_vault_recall", driver_text)
        self.assertIn("perseus_vault_context", driver_text)
        self.assertIn("--offline", driver_text)
        self.assertNotIn("requests.", driver_text)


if __name__ == "__main__":
    unittest.main()
