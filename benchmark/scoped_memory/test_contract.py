"""Executable tests for the portable scoped-memory contract (#1103)."""
from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from benchmark.scoped_memory.contract import (
    CONTRACT_VERSION,
    EXPECTED_CASES,
    InProcessSurface,
    RecordingRanker,
    TrustedAuthority,
    TrustedScope,
    ContractValidationError,
    execute_contract,
    load_fixture,
    projection_digest,
    verify_projection,
)

FIXTURE = Path(__file__).with_name("fixture.json")


class ScopedMemoryContractTests(unittest.TestCase):
    def test_in_process_contract_is_complete_and_explicit(self):
        surface = InProcessSurface()
        run = execute_contract(surface, fixture=load_fixture(FIXTURE))

        self.assertTrue(run.passed, run.failures)
        self.assertEqual(run.contract_version, CONTRACT_VERSION)
        self.assertEqual(set(run.outcomes), set(EXPECTED_CASES))
        self.assertEqual(
            {row["outcome"] for row in run.outcomes.values()},
            {"allow", "deny", "scope_mismatch", "stale_conflict", "abstain", "unavailable"},
        )
        self.assertEqual(len(run.projection["projection_sha256"]), 64)
        self.assertEqual(len(run.projection["receipt_sha256"]), 64)
        self.assertFalse(run.projection["raw_inputs_captured"])
        self.assertNotIn("body", json.dumps(run.projection, sort_keys=True).lower())
        self.assertNotIn("prompt", json.dumps(run.projection, sort_keys=True).lower())

    def test_scope_is_injected_by_host_and_ranker_sees_only_authorized_candidates(self):
        ranker = RecordingRanker()
        run = execute_contract(InProcessSurface(), fixture=load_fixture(FIXTURE), ranker=ranker)

        self.assertEqual(run.outcomes["scope-injection" ]["outcome"], "deny")
        self.assertEqual(run.outcomes["scope-injection"]["reason"], "caller_scope_injection")
        self.assertTrue(ranker.seen)
        self.assertEqual(ranker.seen[:3], sorted(ranker.seen[:3]))
        self.assertTrue(set(ranker.seen).issubset({"anchor-a", "correctable", "supersede-old"}))
        self.assertNotIn("anchor-b", ranker.seen)
        self.assertNotIn("anchor-session", ranker.seen)
        self.assertTrue(run.outcomes["cross-scope-search"]["checks"]["ranker_after_scope_filter"])

    def test_authority_lineage_and_observable_statuses(self):
        run = execute_contract(InProcessSurface(), fixture=load_fixture(FIXTURE))

        self.assertEqual(run.outcomes["read-only-write"]["outcome"], "deny")
        self.assertEqual(run.outcomes["authorized-store"]["outcome"], "allow")
        correction = run.outcomes["correction-lineage"]
        self.assertTrue(correction["checks"]["old_record_retained"])
        self.assertTrue(correction["checks"]["successor_explicit"])
        self.assertTrue(correction["checks"]["successor_active"])
        supersession = run.outcomes["supersession-lineage"]
        self.assertTrue(supersession["checks"]["old_record_retained"])
        self.assertTrue(supersession["checks"]["old_is_superseded"])
        self.assertTrue(supersession["checks"]["successor_active"])
        self.assertEqual(run.outcomes["stale-conflict"]["outcome"], "stale_conflict")

    def test_replay_projection_is_byte_stable_and_detects_digest_mismatch(self):
        fixture = load_fixture(FIXTURE)
        first = execute_contract(InProcessSurface(), fixture=fixture)
        second = execute_contract(InProcessSurface(), fixture=copy.deepcopy(fixture))

        self.assertEqual(first.projection, second.projection)
        self.assertEqual(projection_digest(first.projection), first.projection["projection_sha256"])
        verify_projection(first.projection)
        tampered = copy.deepcopy(first.projection)
        tampered["receipt_sha256"] = "0" * 64
        with self.assertRaises(ContractValidationError):
            verify_projection(tampered)

    def test_fixture_rejects_missing_malformed_and_traversal_values(self):
        fixture = load_fixture(FIXTURE)
        missing = copy.deepcopy(fixture)
        del missing["trusted_scope"]
        with self.assertRaises(ContractValidationError):
            load_fixture(missing)
        malformed = copy.deepcopy(fixture)
        malformed["records"] = "not-a-list"
        with self.assertRaises(ContractValidationError):
            load_fixture(malformed)
        traversal = copy.deepcopy(fixture)
        traversal["trusted_scope"]["workspace_hash"] = "../outside"
        with self.assertRaises(ContractValidationError):
            load_fixture(traversal)
        bad_digest = copy.deepcopy(fixture)
        bad_digest["records"][0]["source_digest"] = "not-a-digest"
        with self.assertRaises(ContractValidationError):
            load_fixture(bad_digest)

    def test_unavailable_surface_is_explicit_not_a_fabricated_pass(self):
        surface = InProcessSurface(available=False)
        run = execute_contract(surface, fixture=load_fixture(FIXTURE))
        self.assertFalse(run.passed)
        self.assertEqual(run.outcomes["surface-unavailable"]["outcome"], "unavailable")
        self.assertEqual(run.projection["status"], "unavailable")
        self.assertNotIn("rate", run.projection)

    def test_trusted_authority_requires_explicit_write_capabilities(self):
        scope = TrustedScope("user-a", "workspace-a", "agent-a", "session-a")
        read_only = TrustedAuthority(scope=scope, allowed_operations=frozenset({"search"}))
        self.assertFalse(read_only.allows("store"))
        self.assertTrue(read_only.allows("search"))
        with self.assertRaises(ContractValidationError):
            TrustedScope("user-a", "workspace-a", "agent-a", "../session")


if __name__ == "__main__":
    unittest.main()
