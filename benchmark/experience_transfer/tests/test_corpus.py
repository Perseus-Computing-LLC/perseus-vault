from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from benchmark.experience_transfer.common import ContractError, canonical_digest, validate_corpus
from benchmark.experience_transfer.generate_corpus import build_corpus


ROOT = Path(__file__).resolve().parents[1]


class CorpusContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads((ROOT / "corpus/corpus.json").read_text(encoding="utf-8"))

    def test_canonical_fixture_validates_and_is_balanced(self):
        validate_corpus(self.corpus)
        self.assertEqual(self.corpus["pair_count"], 24)
        self.assertEqual(sum(self.corpus["category_counts"].values()), 24)
        self.assertEqual(sum(case["controls"]["negative_control"] for case in self.corpus["cases"]), 15)

    def test_generator_is_byte_stable(self):
        first = json.dumps(build_corpus(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        second = json.dumps(build_corpus(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        committed = (ROOT / "corpus/corpus.json").read_text(encoding="utf-8")
        self.assertEqual(first, second)
        self.assertEqual(first, committed)
        self.assertEqual(canonical_digest(json.loads(first)), canonical_digest(self.corpus))

    def test_agent_view_does_not_contain_evaluation_label(self):
        forbidden = {"expected_decision_class", "expected_reason_code", "risk_case", "label_sha256"}
        for case in self.corpus["cases"]:
            view_text = json.dumps(case["agent_view"], sort_keys=True)
            self.assertTrue(forbidden.isdisjoint(view_text.split()))
            self.assertNotIn("expected_decision_class", view_text)
            self.assertNotIn("expected_reason_code", view_text)
            self.assertNotIn("negative_control", view_text)
            self.assertNotIn("control_type", view_text)
            self.assertNotIn("fresh-runbook", view_text)
            self.assertNotIn("revoked-source", view_text)

    def test_duplicate_case_id_fails_closed(self):
        broken = copy.deepcopy(self.corpus)
        broken["cases"][1]["case_id"] = broken["cases"][0]["case_id"]
        with self.assertRaises(ContractError):
            validate_corpus(broken)

    def test_tampered_world_hash_fails_closed(self):
        broken = copy.deepcopy(self.corpus)
        broken["cases"][0]["world_history"][1]["facts_code"] = "tampered_world"
        with self.assertRaises(ContractError):
            validate_corpus(broken)

    def test_tampered_evidence_commitment_fails_closed(self):
        broken = copy.deepcopy(self.corpus)
        broken["cases"][0]["experience"]["evidence"][0]["status"] = "revoked"
        with self.assertRaises(ContractError):
            validate_corpus(broken)

    def test_unauthorized_transition_is_not_accepted_chain(self):
        broken = copy.deepcopy(self.corpus)
        broken["cases"][0]["transition_attempts"] = [{
            "state": "execute_change",
            "event_id": "evt-test-unauthorized",
            "authorized": False,
            "expected_disposition": "accepted",
        }]
        with self.assertRaises(ContractError):
            validate_corpus(broken)

    def test_each_case_has_bound_lifecycle_and_commitments(self):
        for case in self.corpus["cases"]:
            self.assertEqual(case["commitments"]["agent_view_sha256"], canonical_digest(case["agent_view"]))
            self.assertEqual(case["commitments"]["world_history_sha256"], canonical_digest(case["world_history"]))
            self.assertEqual(case["transition_chain"][0]["state"], "captured")
            self.assertEqual(case["transition_chain"][3]["state"], "transferred")


if __name__ == "__main__":
    unittest.main()
