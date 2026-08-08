import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from run import correction_signature


class CorrectionHarnessTests(unittest.TestCase):
    def test_correction_signature_accepts_rows_and_is_order_independent(self):
        rows = [
            {"case": "replacement", "axis": "A_current_answer", "ok": True, "detail": "private"},
            {"case": "replacement", "axis": "B_unqualified_stale_absent", "ok": False, "detail": "private"},
        ]
        reversed_rows = list(reversed(rows))
        first = correction_signature(rows)
        second = correction_signature(reversed_rows)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertTrue(all(char in "0123456789abcdef" for char in first))

    def test_correction_signature_excludes_private_details(self):
        rows = [{"case": "replacement", "axis": "A_current_answer", "ok": True, "detail": "private one"}]
        changed = [{"case": "replacement", "axis": "A_current_answer", "ok": True, "detail": "private two"}]
        self.assertEqual(correction_signature(rows), correction_signature(changed))


if __name__ == "__main__":
    unittest.main()
