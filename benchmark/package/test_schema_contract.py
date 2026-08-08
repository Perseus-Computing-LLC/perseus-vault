import json
import unittest
from pathlib import Path


class SchemaContractTests(unittest.TestCase):
    def test_control_profile_and_report_schemas_are_valid_json(self):
        root = Path(__file__).parent
        control = json.loads((root / "control_profile.schema.json").read_text())
        report = json.loads((root / "report.schema.json").read_text())
        self.assertEqual(control["type"], "object")
        self.assertTrue(control["additionalProperties"] is False)
        self.assertEqual(report["type"], "object")
        self.assertTrue(report["additionalProperties"] is False)
        self.assertEqual(report["properties"]["raw_inputs_captured"]["const"], False)
        self.assertIn("run_fingerprint_sha256", report["required"])

    def test_report_schema_declares_safe_shape_constraints(self):
        schema = json.loads((Path(__file__).parent / "report.schema.json").read_text())
        defs = schema["$defs"]
        self.assertIn("publicIdentifier", defs)
        self.assertEqual(schema["$defs"]["metric"]["properties"]["denominator"]["minimum"], 1)
        self.assertEqual(schema["$defs"]["case"]["properties"]["checks"]["minProperties"], 1)


if __name__ == "__main__":
    unittest.main()
