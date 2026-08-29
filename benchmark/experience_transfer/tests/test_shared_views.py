from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from benchmark.experience_transfer.common import ContractError, canonical_digest, validate_shared_views


ROOT = Path(__file__).resolve().parents[1]


class SharedViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = json.loads((ROOT / "corpus/shared_agent_views.json").read_text(encoding="utf-8"))

    def test_shared_projection_validates_and_has_opaque_ids(self):
        validate_shared_views(self.bundle)
        self.assertEqual(len(self.bundle["cases"]), 24)
        text = json.dumps(self.bundle, sort_keys=True).lower()
        for marker in (
            "expected_decision_class", "expected_reason_code", "negative_control",
            "control_type", "fresh-runbook", "revoked-source",
        ):
            self.assertNotIn(marker, text)

    def test_shared_projection_digest_is_bound(self):
        for row in self.bundle["cases"]:
            self.assertEqual(row["agent_view_sha256"], canonical_digest(row["agent_view"]))

    def test_shared_projection_rejects_label_leak(self):
        broken = copy.deepcopy(self.bundle)
        broken["cases"][0]["agent_view"]["label"] = "reuse"
        with self.assertRaises(ContractError):
            validate_shared_views(broken)


if __name__ == "__main__":
    unittest.main()
