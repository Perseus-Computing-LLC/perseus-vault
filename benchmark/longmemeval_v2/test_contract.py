from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from benchmark.longmemeval_v2.adapter import AdapterContractError, LongMemEvalV2VaultMemory
from benchmark.longmemeval_v2.replay import (
    FORBIDDEN_REPORT_MARKERS,
    ReplayContractError,
    canonical_sha256,
    load_fixture,
    replay_fixture,
    run_replay,
    validate_replay_report,
)

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "synthetic_v2.json"
CONFIG = ROOT / "provider_free_config.json"


def trajectory(trajectory_id: str, states: list[dict], *, scope: str = "workspace-a") -> dict:
    return {
        "id": trajectory_id,
        "session_id": f"session-{trajectory_id}",
        "scope": scope,
        "domain": "web",
        "goal": "synthetic goal",
        "states": states,
        # This field must not enter the adapter's allow-listed representation.
        "question_id": "forbidden-question-id",
    }


class AdapterBoundaryTests(unittest.TestCase):
    def make_adapter(self, **overrides: object) -> LongMemEvalV2VaultMemory:
        params = {
            "scope": "workspace-a",
            "allowed_image_root": str(ROOT.parent.parent),
            "max_results": 4,
            "max_text_chars": 320,
            "max_total_text_chars": 900,
            "max_image_items": 2,
            "available": True,
        }
        params.update(overrides)
        return LongMemEvalV2VaultMemory(params)

    def test_v2_boundary_has_exact_insert_and_query_shape(self):
        insert = inspect.signature(LongMemEvalV2VaultMemory.insert)
        query = inspect.signature(LongMemEvalV2VaultMemory.query)
        self.assertEqual(list(insert.parameters), ["self", "trajectory"])
        self.assertEqual(list(query.parameters), ["self", "query", "query_image"])
        self.assertIsNone(query.parameters["query_image"].default)

    def test_identity_and_event_order_are_preserved(self):
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-order", [
            {"timestamp": "2026-08-01T00:00:00Z", "source_refs": ["src-a"], "text": "deploy opened"},
            {"timestamp": "2026-08-02T00:00:00Z", "source_refs": ["src-b"], "text": "deploy confirmed"},
        ]))
        items = adapter.query("deploy")
        self.assertEqual([item["type"] for item in items], ["text", "text"])
        self.assertIn("trajectory_id=traj-order", items[0]["value"])
        self.assertIn("session_id=session-traj-order", items[0]["value"])
        self.assertIn("event_index=0", items[0]["value"])
        self.assertIn("event_index=1", items[1]["value"])
        self.assertIn("source_refs=src-a", items[0]["value"])
        self.assertIn("source_refs=src-b", items[1]["value"])

    def test_scope_filtering_excludes_other_workspace(self):
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-local", [{"text": "deploy local", "scope": "workspace-a"}]))
        adapter.insert(trajectory("traj-other", [{"text": "deploy other", "scope": "workspace-b"}], scope="workspace-b"))
        items = adapter.query("deploy")
        self.assertEqual(len(items), 1)
        self.assertIn("traj-local", items[0]["value"])
        self.assertNotIn("traj-other", items[0]["value"])
        self.assertEqual(adapter.post_query_hook(query="deploy", query_image=None, memory_context=items)["excluded"]["scope"], 1)

    def test_conflicting_evidence_is_visible_without_resolution(self):
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-a", [{"text": "checkout uses button alpha", "conflict_id": "conflict-1"}]))
        adapter.insert(trajectory("traj-b", [{"text": "checkout uses button beta", "conflict_id": "conflict-1"}]))
        items = adapter.query("checkout button")
        self.assertEqual(len(items), 2)
        joined = "\n".join(item["value"] for item in items)
        self.assertIn("conflict_ids=conflict-1", joined)
        self.assertIn("button alpha", joined)
        self.assertIn("button beta", joined)
        self.assertEqual(adapter.post_query_hook(query="checkout button", query_image=None, memory_context=items)["conflicts_visible"], 2)

    def test_stale_and_superseded_records_are_excluded(self):
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-stale", [{"text": "deploy stale", "lifecycle": "stale"}]))
        adapter.insert(trajectory("traj-superseded", [
            {"event_id": "old", "text": "deploy old", "superseded_by": "new"},
            {"event_id": "new", "text": "deploy new", "lifecycle": "active"},
        ]))
        items = adapter.query("deploy")
        joined = "\n".join(item["value"] for item in items)
        self.assertNotIn("traj-stale", joined)
        self.assertNotIn("deploy old", joined)
        self.assertIn("deploy new", joined)
        diagnostic = adapter.post_query_hook(query="deploy", query_image=None, memory_context=items)
        self.assertGreaterEqual(diagnostic["excluded"]["lifecycle"], 1)
        self.assertGreaterEqual(diagnostic["excluded"]["superseded"], 1)

    def test_forward_supersedes_link_excludes_the_referenced_prior_event(self):
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-forward-supersede", [
            {"event_id": "old", "text": "deploy old"},
            {"event_id": "new", "text": "deploy new", "supersedes": ["old"]},
        ]))
        values = [item["value"] for item in adapter.query("deploy") if item["type"] == "text"]
        self.assertEqual(len(values), 1)
        self.assertIn("event_id=new", values[0])
        self.assertGreaterEqual(adapter.diagnostic()["excluded"]["superseded"], 1)

    def test_out_of_order_timestamps_do_not_reorder_event_or_source_identity(self):
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-state-index", [
            {"state_index": 7, "timestamp": "2026-08-02T00:00:00Z", "url": "https://example.invalid/late", "text": "deploy late"},
            {"state_index": 3, "timestamp": "2026-08-01T00:00:00Z", "url": "https://example.invalid/early", "text": "deploy early"},
        ]))
        values = [item["value"] for item in adapter.query("deploy") if item["type"] == "text"]
        self.assertEqual([next(part for part in value.split("; ") if part.startswith("event_index=")).split("=", 1)[1] for value in values], ["0", "1"])
        self.assertIn("state_index=7", values[0])
        self.assertIn("state_index=3", values[1])
        self.assertIn("https://example.invalid/late", values[0])
        self.assertIn("https://example.invalid/early", values[1])

    def test_empty_session_and_duplicate_event_identity_fail_closed(self):
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert({"id": "bad-session", "session_id": "", "scope": "workspace-a", "states": [{"text": "evidence"}]})
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert(trajectory("duplicate-events", [{"event_id": "same", "text": "one"}, {"event_id": "same", "text": "two"}]))
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert(trajectory("dangling-supersedes", [{"event_id": "new", "text": "deploy", "supersedes": ["missing"]}]))
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert(trajectory("conflicting-supersedes", [
                {"event_id": "old", "text": "deploy old"},
                {"event_id": "new-a", "text": "deploy a", "supersedes": ["old"]},
                {"event_id": "new-b", "text": "deploy b", "supersedes": ["old"]},
            ]))
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert(trajectory("dangling-superseded-by", [
                {"event_id": "old", "text": "deploy old", "superseded_by": "missing"},
            ]))
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert(trajectory("self-superseded-by", [
                {"event_id": "old", "text": "deploy old", "superseded_by": "old"},
            ]))
        with self.assertRaises(AdapterContractError):
            self.make_adapter().insert(trajectory("reverse-forward-conflict", [
                {"event_id": "old", "text": "deploy old", "superseded_by": "new-a"},
                {"event_id": "new-a", "text": "deploy a"},
                {"event_id": "new-b", "text": "deploy b", "supersedes": ["old"]},
            ]))
        self.make_adapter().insert(trajectory("matching-reverse-forward", [
            {"event_id": "old", "text": "deploy old", "superseded_by": "new"},
            {"event_id": "new", "text": "deploy new", "supersedes": ["old"]},
        ]))

    def test_context_is_bounded(self):
        adapter = self.make_adapter(max_text_chars=120, max_total_text_chars=240)
        adapter.insert(trajectory("traj-long", [{"text": "deploy " + ("very-long " * 200)}]))
        items = adapter.query("deploy")
        text_items = [item for item in items if item["type"] == "text"]
        self.assertTrue(text_items)
        self.assertTrue(all(len(item["value"]) <= 120 for item in text_items))
        self.assertLessEqual(sum(len(item["value"]) for item in text_items), 240)
        self.assertTrue(adapter.post_query_hook(query="deploy", query_image=None, memory_context=items)["bounded"])

    def test_multimodal_evidence_returns_bounded_image_item(self):
        image = ROOT / "fixtures" / "synthetic-screenshot.png"
        adapter = self.make_adapter()
        adapter.insert(trajectory("traj-image", [{"text": "checkout screenshot", "screenshot": str(image)}]))
        items = adapter.query("checkout", query_image=str(image))
        self.assertEqual([item["type"] for item in items], ["text", "image"])
        self.assertEqual(items[1]["value"], str(image.resolve()))

    def test_unavailable_and_abstained_outcomes_are_explicit(self):
        unavailable = self.make_adapter(available=False)
        unavailable.insert(trajectory("traj-unavailable", [{"text": "deploy"}]))
        self.assertEqual(unavailable.query("deploy"), [])
        self.assertEqual(unavailable.post_query_hook(query="deploy", query_image=None, memory_context=[])["status"], "unavailable")

        abstained = self.make_adapter()
        abstained.insert(trajectory("traj-abstained", [{"text": "different evidence"}]))
        self.assertEqual(abstained.query("absent fact"), [])
        self.assertEqual(abstained.post_query_hook(query="absent fact", query_image=None, memory_context=[])["status"], "abstained")

    def test_unknown_benchmark_fields_do_not_persist(self):
        adapter = self.make_adapter()
        payload = trajectory("traj-gold-blind", [{"text": "safe evidence"}])
        payload.update({"question_type": "dynamic", "answer_session_ids": ["gold-session"], "gold_answer": "gold"})
        adapter.insert(payload)
        serialized = json.dumps(adapter.debug_public_state(), sort_keys=True)
        for marker in ("question_type", "answer_session_ids", "gold_answer", "forbidden-question-id", "gold-session"):
            self.assertNotIn(marker, serialized)


class ReplayContractTests(unittest.TestCase):
    def test_fixture_covers_required_provider_free_cases(self):
        fixture = load_fixture(FIXTURE)
        case_ids = {case["case_id"] for case in fixture["cases"]}
        self.assertTrue({"empty-evidence", "stale-evidence", "conflicting-evidence", "missing-evidence", "multimodal-evidence", "long-haystack", "scope-filtering", "chronological-updates", "superseded-records", "unavailable", "abstained"}.issubset(case_ids))
        self.assertEqual({case["case_id"] for case in fixture["negative_cases"]}, {"dangling-reverse-superseded-by", "self-reverse-superseded-by", "conflicting-reverse-superseded-by"})

    def test_fixture_negative_supersession_cases_fail_closed(self):
        fixture = load_fixture(FIXTURE)
        for negative in fixture["negative_cases"]:
            with self.assertRaises(AdapterContractError):
                LongMemEvalV2VaultMemory({"scope": "workspace-a"}).insert(negative["trajectory"])

    def test_gold_fields_never_enter_adapter_call_path(self):
        fixture = load_fixture(FIXTURE)
        case = next(case for case in fixture["cases"] if case["case_id"] == "gold-blind")
        calls: list[object] = []

        class GuardedAdapter(LongMemEvalV2VaultMemory):
            def insert(self, trajectory):  # type: ignore[no-untyped-def]
                calls.append(("insert", trajectory))
                return super().insert(trajectory)

            def query(self, query, query_image=None):  # type: ignore[no-untyped-def]
                calls.append(("query", query, query_image))
                return super().query(query, query_image)

        replay_fixture(case, GuardedAdapter({"scope": "workspace-a", "allowed_image_root": str(ROOT.parent.parent)}))
        encoded = json.dumps(calls, sort_keys=True)
        for marker in ("question_id", "question_type", "answer_session_ids", "gold_answer", "evaluator_metadata", "hidden_label"):
            self.assertNotIn(marker, encoded)

    def test_replay_strips_benchmark_metadata_before_adapter_insert(self):
        case = {
            "case_id": "gold-boundary",
            "ability": "static_state_recall",
            "domain": "web",
            "question_id": "question-gold",
            "question_type": "static-environment",
            "answer_session_ids": ["session-gold"],
            "gold_answer": "gold must not reach insert",
            "evaluator_metadata": {"label": "hidden"},
            "hidden_label": "gold",
            "trajectories": [{
                "id": "traj-gold-boundary",
                "session_id": "session-gold-boundary",
                "scope": "workspace-a",
                "label": "nested-label",
                "unknown_hidden_field": "must-not-enter-adapter",
                "question_id": "nested-question-gold",
                "answer_session_ids": ["nested-gold"],
                "states": [{"text": "safe evidence", "label": "state-label", "unknown_state_field": "hidden"}],
            }],
            "query": "safe evidence",
            "query_image": None,
            "expected": {"status": "complete", "min_text_items": 1},
        }
        calls: list[object] = []

        class GuardedAdapter(LongMemEvalV2VaultMemory):
            def insert(self, trajectory):  # type: ignore[no-untyped-def]
                calls.append(trajectory)
                return super().insert(trajectory)

        replay_fixture(case, GuardedAdapter({"scope": "workspace-a", "allowed_image_root": str(ROOT.parent.parent)}))
        encoded = json.dumps(calls, sort_keys=True)
        for marker in ("question_id", "question_type", "answer_session_ids", "gold_answer", "evaluator_metadata", "hidden_label", "nested-label", "unknown_hidden_field", "state-label", "unknown_state_field"):
            self.assertNotIn(marker, encoded)

    def test_replay_emits_five_ability_scorecard_and_separate_metrics(self):
        with tempfile.TemporaryDirectory(prefix="lme-v2-report-") as temp:
            result = run_replay(FIXTURE, Path(temp), repo_root=ROOT.parent.parent)
            report = json.loads((Path(temp) / "replay_report.json").read_text(encoding="utf-8"))
            validate_replay_report(report)
            self.assertEqual(set(report["ability_scorecard"]), {"static_state_recall", "dynamic_state_tracking", "workflow_knowledge", "environment_gotchas", "premise_awareness"})
            self.assertIn("retrieval", report["metrics"])
            self.assertIn("context", report["metrics"])
            self.assertIn("answer", report["metrics"])
            self.assertIn("cost", report["metrics"])
            self.assertIn("instrumentation", report["metrics"])
            self.assertEqual(report["provider_calls"], 0)
            self.assertEqual(report["network_calls"], 0)
            self.assertEqual(report["model_calls"], 0)
            self.assertEqual(report["judge_calls"], 0)
            self.assertEqual(result["case_count"], len(report["cases"]))

    def test_manifest_binds_all_provider_free_readiness_inputs(self):
        with tempfile.TemporaryDirectory(prefix="lme-v2-manifest-") as temp:
            result = run_replay(FIXTURE, Path(temp), repo_root=ROOT.parent.parent)
            manifest = json.loads((Path(temp) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["benchmark"]["revision"], "2cc8c540bdb87fe6761629b585e727e1c4704520")
            self.assertRegex(manifest["benchmark"]["harness_source_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["dataset"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["vault"]["binary_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["vault"]["schema_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["adapter"]["configuration_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["token_budgets_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["execution_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(manifest["offline"]["provider_calls"], 0)
            inventory = json.loads((Path(temp) / "artifact_inventory.json").read_text(encoding="utf-8"))
            inventory_paths = {item["path"] for item in inventory["generated_artifacts"]}
            self.assertIn("benchmark/longmemeval_v2/fixtures/synthetic-screenshot.png", inventory_paths)
            self.assertEqual(result["provider_calls"], 0)

    def test_report_validator_rejects_malformed_nested_contracts(self):
        with tempfile.TemporaryDirectory(prefix="lme-v2-invalid-report-") as temp:
            run_replay(FIXTURE, Path(temp), repo_root=ROOT.parent.parent)
            report = json.loads((Path(temp) / "replay_report.json").read_text(encoding="utf-8"))
            broken = json.loads(json.dumps(report))
            broken["cases"][0]["retrieval"]["status"] = "made_up"
            unsigned = dict(broken)
            unsigned.pop("run_signature_sha256")
            broken["run_signature_sha256"] = canonical_sha256(unsigned)
            with self.assertRaises(ReplayContractError):
                validate_replay_report(broken)

            broken = json.loads(json.dumps(report))
            broken["metrics"]["answer"]["accuracy"] = 1.0
            unsigned = dict(broken)
            unsigned.pop("run_signature_sha256")
            broken["run_signature_sha256"] = canonical_sha256(unsigned)
            with self.assertRaises(ReplayContractError):
                validate_replay_report(broken)

    def test_report_validator_reconciles_forged_aggregates_with_case_rows(self):
        with tempfile.TemporaryDirectory(prefix="lme-v2-aggregate-report-") as temp:
            run_replay(FIXTURE, Path(temp), repo_root=ROOT.parent.parent)
            report = json.loads((Path(temp) / "replay_report.json").read_text(encoding="utf-8"))

            def assert_rejected(mutator):
                broken = json.loads(json.dumps(report))
                mutator(broken)
                unsigned = dict(broken)
                unsigned.pop("run_signature_sha256")
                broken["run_signature_sha256"] = canonical_sha256(unsigned)
                with self.assertRaises(ReplayContractError):
                    validate_replay_report(broken)

            assert_rejected(lambda value: value["ability_scorecard"]["static_state_recall"].update(
                case_count=value["ability_scorecard"]["static_state_recall"]["case_count"] + 1,
                provider_free_ready_cases=value["ability_scorecard"]["static_state_recall"]["provider_free_ready_cases"] + 1,
                context_bounded_cases=value["ability_scorecard"]["static_state_recall"]["context_bounded_cases"] + 1,
                status_counts={"complete": value["ability_scorecard"]["static_state_recall"]["case_count"] + 1},
            ))
            assert_rejected(lambda value: value["metrics"]["retrieval"].update(
                evidence_cases=value["metrics"]["retrieval"]["evidence_cases"] + 1,
            ))
            assert_rejected(lambda value: value["metrics"]["context"].update(
                text_items=value["metrics"]["context"]["text_items"] + 1,
            ))
            assert_rejected(lambda value: value["metrics"]["instrumentation"].update(
                conflicting_cases=value["metrics"]["instrumentation"]["conflicting_cases"] + 1,
            ))
            assert_rejected(lambda value: value["claim_boundary"]["supported"].append("customer efficacy proven"))

    def test_replay_artifacts_are_byte_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="lme-v2-determinism-") as temp:
            first = Path(temp) / "first"
            second = Path(temp) / "second"
            run_replay(FIXTURE, first, repo_root=ROOT.parent.parent)
            run_replay(FIXTURE, second, repo_root=ROOT.parent.parent)
            for name in ("manifest.json", "replay_report.json", "replay_signature.txt", "artifact_inventory.json"):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_report_has_no_raw_benchmark_or_provider_fields(self):
        with tempfile.TemporaryDirectory(prefix="lme-v2-safe-") as temp:
            run_replay(FIXTURE, Path(temp), repo_root=ROOT.parent.parent)
            serialized = (Path(temp) / "replay_report.json").read_text(encoding="utf-8").lower()
            for marker in FORBIDDEN_REPORT_MARKERS:
                self.assertNotIn(marker, serialized)


if __name__ == "__main__":
    unittest.main()
