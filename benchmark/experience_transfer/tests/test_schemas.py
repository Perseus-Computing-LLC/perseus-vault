from __future__ import annotations

import json
import unittest
from pathlib import Path

from benchmark.experience_transfer.common import ACCEPTANCE_SCHEMA, ADAPTER_CONTRACT_VERSION, CORPUS_SCHEMA, MANIFEST_SCHEMA, REPORT_SCHEMA, SHARED_VIEWS_SCHEMA


ROOT = Path(__file__).resolve().parents[1]


class SchemaDocumentTests(unittest.TestCase):
    def test_schema_documents_are_valid_json_and_named(self):
        expected = {
            "corpus.schema.json": CORPUS_SCHEMA,
            "adapter-contract.schema.json": ADAPTER_CONTRACT_VERSION,
            "run-manifest.schema.json": MANIFEST_SCHEMA,
            "public-report.schema.json": REPORT_SCHEMA,
            "shared-agent-views.schema.json": SHARED_VIEWS_SCHEMA,
            "acceptance.schema.json": ACCEPTANCE_SCHEMA,
        }
        for name, identifier in expected.items():
            document = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            self.assertEqual(document["type"], "object")
            self.assertEqual(document["additionalProperties"], False)
            self.assertIn("required", document)
            self.assertIn(identifier.split("/", 1)[0], document["$id"])


if __name__ == "__main__":
    unittest.main()
