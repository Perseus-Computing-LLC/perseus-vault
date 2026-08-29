from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmark.experience_transfer.common import reject_forbidden
from benchmark.experience_transfer.reference_workflow.implementation import WORKFLOW_ID, build_receipt, run


ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads((ROOT / "corpus/corpus.json").read_text(encoding="utf-8"))


class ReferenceWorkflowTests(unittest.TestCase):
    def test_receipt_covers_reuse_reject_abstain_block(self):
        wanted = {
            "vet-01": ("reuse", "answer_from_verified_experience"),
            "vet-04": ("reject", "reject_stale_or_failed_experience"),
            "vet-13": ("abstain", "abstain_without_sufficient_evidence"),
            "vet-17": ("block", "block_before_action"),
        }
        for case in CORPUS["cases"]:
            if case["case_id"] not in wanted:
                continue
            receipt = build_receipt(case)
            decision, outcome = wanted[case["case_id"]]
            self.assertEqual(receipt["decision"], decision)
            self.assertEqual(receipt["outcome"], outcome)
            self.assertEqual(receipt["receipt_sha256"], hashlib.sha256(json.dumps({k: v for k, v in receipt.items() if k != "receipt_sha256"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest())
            reject_forbidden(receipt)

    def test_workflow_report_is_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="vet-workflow-") as temp:
            one = Path(temp) / "one.json"
            two = Path(temp) / "two.json"
            run(ROOT / "corpus/corpus.json", one)
            run(ROOT / "corpus/corpus.json", two)
            self.assertEqual(one.read_bytes(), two.read_bytes())
            report = json.loads(one.read_text(encoding="utf-8"))
            self.assertEqual(report["workflow_id"], WORKFLOW_ID)
            self.assertEqual(report["provider_calls"], 0)
            self.assertEqual(len(report["receipts"]), 4)


if __name__ == "__main__":
    unittest.main()
