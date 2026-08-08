import sqlite3
import tempfile
import unittest
from pathlib import Path

from metrics import resource_overlay, sqlite_bytes, token_cost, token_proxy


class EconomicsMetricTests(unittest.TestCase):
    def test_token_proxy_and_explicit_cost_are_deterministic(self):
        self.assertEqual(token_proxy("12345678"), 2)
        self.assertEqual(token_cost(1_000_000, 500_000, input_usd_per_million=2.0, output_usd_per_million=4.0), 4.0)
        self.assertIsNone(token_cost(10))

    def test_resource_overlay_reports_storage_counts_and_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            connection = sqlite3.connect(db)
            connection.execute("CREATE TABLE entities (id TEXT)")
            connection.execute("CREATE TABLE entity_history (id TEXT)")
            connection.execute("CREATE TABLE journal (id TEXT)")
            connection.execute("CREATE TABLE links (id TEXT)")
            connection.executemany("INSERT INTO entities VALUES (?)", [("a",), ("b",)])
            connection.commit()
            connection.close()
            overlay = resource_overlay(db_path=db, injected_text="12345678")
            self.assertEqual(overlay["counts"]["entities"], 2)
            self.assertEqual(overlay["tokens"]["input_proxy"], 2)
            self.assertGreaterEqual(overlay["storage"]["total_bytes"], overlay["storage"]["db_bytes"])

    def test_sqlite_bytes_includes_sidecars_when_present(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "metrics.db"
            db.write_bytes(b"db")
            Path(f"{db}-wal").write_bytes(b"wal")
            sizes = sqlite_bytes(db)
            self.assertEqual(sizes["db_bytes"], 2)
            self.assertEqual(sizes["wal_bytes"], 3)
            self.assertEqual(sizes["total_bytes"], 5)


if __name__ == "__main__":
    unittest.main()
