import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("scale", Path(__file__).with_name("run.py"))
assert spec and spec.loader
scale = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scale)


import unittest


class ScaleEvaluationTests(unittest.TestCase):
    def test_evaluate_requires_all_agents_to_retrieve_their_shared_fact(self):
        report = {"agents": 3, "results": [
            {"agent": "a", "found_shared": True, "private_hidden": True, "latency_ms": 2.0},
            {"agent": "b", "found_shared": True, "private_hidden": True, "latency_ms": 3.0},
            {"agent": "c", "found_shared": False, "private_hidden": True, "latency_ms": 4.0},
        ]}
        verdict = scale.evaluate(report)
        self.assertEqual(verdict["retrieval_quality"], 2 / 3)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["latency"]["p95_ms"], 4.0)

    def test_evaluate_passes_when_quality_and_latency_budgets_hold(self):
        report = {"agents": 2, "results": [
            {"agent": "a", "found_shared": True, "private_hidden": True, "latency_ms": 1.0},
            {"agent": "b", "found_shared": True, "private_hidden": True, "latency_ms": 2.0},
        ]}
        self.assertTrue(scale.evaluate(report, latency_budget_ms=5)["passed"])
