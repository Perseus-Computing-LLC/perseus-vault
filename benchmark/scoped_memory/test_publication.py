"""Publication-boundary tests for the scoped-memory contract."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from benchmark.scoped_memory.contract import InProcessSurface, execute_contract, load_fixture
from benchmark.scoped_memory.run import publish_run

FIXTURE = Path(__file__).with_name("fixture.json")


class ScopedMemoryPublicationTests(unittest.TestCase):
    def test_report_is_hash_only_and_repeated_signature_is_stable(self):
        fixture = load_fixture(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "contract-anchor"
            binary.write_bytes(b"scoped-memory-contract-anchor-v1")
            first = publish_run(
                execute_contract(InProcessSurface(), fixture=fixture),
                fixture=fixture,
                surface_name="inprocess",
                binary=binary,
            )
            second = publish_run(
                execute_contract(InProcessSurface(), fixture=fixture),
                fixture=fixture,
                surface_name="inprocess",
                binary=binary,
            )
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["result_signature_sha256"], second["result_signature_sha256"])
        self.assertEqual(first["cases"], second["cases"])
        self.assertEqual(first["public_evidence"], "hash-only")
        self.assertFalse(first["raw_inputs_captured"])
        cases = first["cases"]
        self.assertIsInstance(cases, list)
        evidence = cast(dict[str, Any], cast(list[dict[str, Any]], cases)[0]["evidence"])
        self.assertIsInstance(evidence, dict)
        self.assertRegex(evidence["digest"], r"^[0-9a-f]{64}$")
        self.assertRegex(evidence["evidence_hash"], r"^[0-9a-f]{64}$")
        public = json.dumps(first, sort_keys=True).lower()
        for forbidden in ("body", "prompt", "query", "token", "credential", "password", "secret", "/opt/data"):
            self.assertNotIn(forbidden, public)

    def test_failed_surface_is_explicitly_partial_not_zero(self):
        fixture = load_fixture(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "contract-anchor"
            binary.write_bytes(b"scoped-memory-contract-anchor-v1")
            run = execute_contract(InProcessSurface(available=False), fixture=fixture)
            report = publish_run(run, fixture=fixture, surface_name="inprocess", binary=binary)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["capabilities"]["inprocess"]["status"], "unavailable")
        self.assertNotIn("rate", report["metrics"]["scoped_memory_contract"])


if __name__ == "__main__":
    unittest.main()
