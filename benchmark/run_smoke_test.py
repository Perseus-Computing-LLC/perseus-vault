import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import run_smoke


class SmokeSummaryTests(unittest.TestCase):
    def test_nested_failed_report_is_not_promoted_to_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            output.write_text(json.dumps({
                "benchmark": "perseus-vault-local-smoke",
                "runs": [{
                    "returncode": 0,
                    "summary": {
                        "passed": False,
                        "checks_passed": 29,
                        "checks_total": 41,
                    },
                }],
                "passed": False,
            }))
            result = {"returncode": 0, "summary": json.loads(output.read_text())}
            self.assertFalse(run_smoke.run_passed(result))
            self.assertFalse(run_smoke.aggregate_passed([result]))

    def test_timeout_result_is_blocking(self):
        result = {"returncode": None, "timeout": True, "summary": {"passed": False}}
        self.assertFalse(run_smoke.run_passed(result))

    def test_nested_passing_report_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            output.write_text(json.dumps({
                "benchmark": "perseus-vault-local-smoke",
                "runs": [{
                    "returncode": 0,
                    "summary": {
                        "passed": True,
                        "status": "passed",
                        "checks_passed": 41,
                        "checks_total": 41,
                        "accuracy": 1.0,
                    },
                }],
                "passed": True,
            }))
            result = {"returncode": 0, "summary": json.loads(output.read_text())}
            self.assertTrue(run_smoke.run_passed(result))


if __name__ == "__main__":
    unittest.main()
