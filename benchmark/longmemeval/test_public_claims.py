import json
import importlib.util
import subprocess
import tempfile
from pathlib import Path
import unittest

from benchmark.longmemeval.public_claims import (
    ACCEPTED_REPORT_SHA256,
    ACCEPTED_MANIFEST_SHA256,
    load_public_claim,
    validate_public_claim,
)


HERE = Path(__file__).resolve().parent
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "gen_benchmark_page", HERE.parent.parent / "scripts" / "gen_benchmark_page.py"
)
assert GENERATOR_SPEC is not None and GENERATOR_SPEC.loader is not None
GENERATOR = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(GENERATOR)


class PublicClaimTests(unittest.TestCase):
    def test_accepted_projection_is_hash_bound_and_complete(self):
        claim = load_public_claim()
        validate_public_claim(claim)
        self.assertEqual(claim["source_report_sha256"], ACCEPTED_REPORT_SHA256)
        self.assertEqual(claim["source_manifest_sha256"], ACCEPTED_MANIFEST_SHA256)
        self.assertEqual(claim["answer_prompt"], "official-cot")
        self.assertEqual(claim["score"], {"correct": 407, "denominator": 500, "accuracy": 0.814})
        self.assertEqual(sum(row["correct"] for row in claim["categories"]), 407)
        self.assertEqual(sum(row["n"] for row in claim["categories"]), 500)
        self.assertFalse(claim["runs_2_3_started"])
        self.assertFalse(claim["preference_structured_included"])

    def test_projection_contains_no_raw_payload_fields(self):
        claim = load_public_claim()
        forbidden = {"body", "response", "hypothesis", "credential", "secret", "password", "api_key", "authorization", "tool_arguments"}
        found = []

        def walk(value, path="$"):
            if isinstance(value, dict):
                for key, child in value.items():
                    lowered = str(key).lower()
                    if lowered in forbidden or any(part in lowered for part in ("body", "response", "credential", "secret", "password", "authorization")):
                        found.append(f"{path}.{key}")
                    walk(child, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{path}[{index}]")

        walk(claim)
        self.assertEqual(found, [])

    def test_generator_renders_single_run_separately_from_historical_means(self):
        claim = load_public_claim()
        rendered = GENERATOR.sec_qa_accepted(claim)
        self.assertIn("81.4%", rendered)
        self.assertIn("single accepted frozen-default run", rendered)
        self.assertIn(ACCEPTED_REPORT_SHA256, rendered)
        self.assertIn(ACCEPTED_MANIFEST_SHA256, rendered)
        self.assertNotIn("mean of 1", rendered)

    def test_cot_fragment_has_balanced_div_markup(self):
        rendered = GENERATOR.sec_qa_cot(
            {"systems": {"perseus-vault": {"accuracy": 0.79}}, "signature_sha256": "0" * 64},
            [],
        )
        self.assertEqual(rendered.count("<div"), rendered.count("</div>"))

    def test_projection_is_stable_json(self):
        path = HERE / "accepted_frozen_default_manifest.json"
        first = json.loads(path.read_text(encoding="utf-8"))
        second = json.loads(json.dumps(first, sort_keys=True))
        self.assertEqual(first, second)

    def test_generator_cli_runs_from_a_clean_checkout_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "benchmarks-index.html"
            result = subprocess.run(
                ["python3", "scripts/gen_benchmark_page.py", "--out", str(output)],
                cwd=HERE.parent.parent,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("81.4%", rendered)
            self.assertIn(ACCEPTED_MANIFEST_SHA256, rendered)
            self.assertNotIn("is in progress", rendered)


if __name__ == "__main__":
    unittest.main()
