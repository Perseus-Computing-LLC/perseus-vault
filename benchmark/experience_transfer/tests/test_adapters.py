from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from benchmark.experience_transfer.adapters import EXECUTABLE_ADAPTERS, ExternalImplementationAdapterSpec, NotMeasured
from benchmark.experience_transfer.common import ContractError, validate_adapter_result


ROOT = Path(__file__).resolve().parents[1]


class AdapterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads((ROOT / "corpus/corpus.json").read_text(encoding="utf-8"))

    def test_executable_adapters_cover_every_case(self):
        for adapter in EXECUTABLE_ADAPTERS:
            for case in self.corpus["cases"]:
                result = adapter.evaluate(case["agent_view"])
                validate_adapter_result(result, case_id=case["case_id"], adapter=adapter.metadata.name)

    def test_governed_policy_matches_every_synthetic_label(self):
        governed = EXECUTABLE_ADAPTERS[-1]
        for case in self.corpus["cases"]:
            result = governed.evaluate(case["agent_view"])
            self.assertEqual(result["decision"], case["evaluation"]["expected_decision_class"], case["case_id"])
            self.assertEqual(result["reason_code"], case["evaluation"]["expected_reason_code"], case["case_id"])

    def test_ungoverned_reference_exhibits_unsafe_reuse(self):
        adapter = EXECUTABLE_ADAPTERS[1]
        unsafe = [
            case["case_id"]
            for case in self.corpus["cases"]
            if adapter.evaluate(case["agent_view"])["unsafe_reuse"]
        ]
        self.assertGreaterEqual(len(unsafe), 10)

    def test_stateless_does_not_read_or_reuse_experience(self):
        adapter = EXECUTABLE_ADAPTERS[0]
        for case in self.corpus["cases"]:
            view = copy.deepcopy(case["agent_view"])
            view["experiences"] = [{"unexpected": "not consumed"}]
            result = adapter.evaluate(view)
            self.assertEqual(result["decision"], "abstain")
            self.assertEqual(result["selected_memory_count"], 0)

    def test_governed_tamper_rejects_input_instead_of_abstaining(self):
        broken = copy.deepcopy(self.corpus["cases"][0]["agent_view"])
        broken["world"]["facts_code"] = "tampered"
        with self.assertRaises(ContractError):
            EXECUTABLE_ADAPTERS[-1].evaluate(broken)

    def test_external_adapter_is_explicitly_not_measured(self):
        adapter = ExternalImplementationAdapterSpec()
        self.assertEqual(adapter.metadata.status, "not_measured")
        with self.assertRaises(NotMeasured):
            adapter.evaluate(self.corpus["cases"][0]["agent_view"])


if __name__ == "__main__":
    unittest.main()
