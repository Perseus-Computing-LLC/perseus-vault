import hashlib
import unittest

from benchmark.hostile_memory_gauntlet.gauntlet.evaluator import aggregate_metrics, evaluate_case, grade_probe
from benchmark.hostile_memory_gauntlet.gauntlet.models import MemoryRecord, RetrievalResult
from benchmark.hostile_memory_gauntlet.gauntlet.providers import ReferenceProvider


def rec(record_id, key, text, *, scope="team-a", valid_from=1, recorded_at=1,
        trust="authoritative", supersedes=()):
    return MemoryRecord(
        record_id=record_id,
        memory_key=key,
        scope=scope,
        text=text,
        source_ref=f"source://{record_id}",
        record_digest=hashlib.sha256(text.encode()).hexdigest(),
        actor="alice",
        trust=trust,
        valid_from=valid_from,
        recorded_at=recorded_at,
        status="active",
        supersedes=tuple(supersedes),
    )


class EvaluatorTests(unittest.TestCase):
    def test_required_evidence_and_provenance_pass(self):
        provider = ReferenceProvider()
        provider.ingest(rec("r1", "profile.alice.role", "Alice is an analyst."))
        probe = {
            "probe_id": "p1", "query": "Alice role", "scope": "team-a", "as_of": 2, "limit": 5,
            "expected": {
                "decision": "answer", "required_ids": ["r1"], "forbidden_ids": [],
                "required_scope": "team-a", "require_provenance": True, "max_context_words": 20,
            },
        }
        result = provider.retrieve("Alice role", "team-a", as_of=2, limit=5)
        observation = grade_probe(probe, result)
        self.assertTrue(observation.passed)
        self.assertEqual(observation.disposition, "correct")

    def test_foreign_hit_is_wrong_not_abstention(self):
        probe = {
            "probe_id": "p1", "query": "role", "scope": "team-a", "as_of": 2, "limit": 5,
            "expected": {
                "decision": "abstain", "required_ids": [], "forbidden_ids": [],
                "required_scope": "team-a", "require_provenance": True, "max_context_words": 20,
            },
        }
        foreign = rec("foreign", "profile.alice.role", "Alice is a doctor.", scope="team-b")
        result = RetrievalResult(decision="answer", hits=(foreign.to_hit(score=1.0),))
        observation = grade_probe(probe, result)
        self.assertFalse(observation.passed)
        self.assertEqual(observation.disposition, "wrong")
        self.assertIn("scope_leak", observation.reason_codes)

    def test_case_evaluation_records_each_probe_without_raw_text(self):
        provider = ReferenceProvider()
        case = {
            "case_id": "case-1", "category": "absence",
            "events": [],
            "probes": [{
                "probe_id": "p1", "query": "unknown", "scope": "team-a", "as_of": 1, "limit": 5,
                "expected": {"decision": "abstain", "required_ids": [], "forbidden_ids": [],
                              "required_scope": "team-a", "require_provenance": True,
                              "max_context_words": 20},
            }],
        }
        result = evaluate_case(provider, case)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.probe_count, 1)
        self.assertNotIn("query", result.to_public_dict())

    def test_unsupported_capability_is_blocked(self):
        class BlockedProvider(ReferenceProvider):
            def retrieve(self, query, scope, as_of, limit):
                return RetrievalResult.blocked("retrieval_not_available")
        provider = BlockedProvider()
        probe = {
            "probe_id": "p1", "query": "x", "scope": "team-a", "as_of": 1, "limit": 5,
            "expected": {"decision": "answer", "required_ids": ["r1"], "forbidden_ids": [],
                          "required_scope": "team-a", "require_provenance": True,
                          "max_context_words": 20},
        }
        observation = grade_probe(probe, provider.retrieve("x", "team-a", 1, 5))
        self.assertFalse(observation.passed)
        self.assertEqual(observation.disposition, "blocked")

    def test_duplicate_replay_materialization_metric_has_a_denominator(self):
        result = {
            "case_id": "dupes", "category": "duplicate_flood", "status": "failed",
            "admissions": [{
                "record_id": "duplicate", "expected_status": "quarantined",
                "admission_status": "admitted", "serveable": True,
                "passed": False, "reason_codes": [],
            }],
            "observations": [],
        }
        metrics = aggregate_metrics([result])
        self.assertEqual(metrics["duplicate_replay_materialization_rate"], 1.0)

    def test_provenance_completeness_does_not_count_empty_answer_probes(self):
        result = {
            "case_id": "missing", "category": "positive", "status": "failed",
            "admissions": [],
            "observations": [{
                "probe_id": "missing", "expected_decision": "answer",
                "disposition": "miss", "passed": False, "required_present": False,
                "provenance_ok": True, "forbidden_present": False, "scope_ok": True,
                "budget_ok": True,
            }],
        }
        metrics = aggregate_metrics([result])
        self.assertEqual(metrics["provenance_completeness_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
