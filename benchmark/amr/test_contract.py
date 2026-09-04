from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmark.amr.profile import (
    AMRValidationError,
    InMemoryAMRStore,
    SUPPORTED_EPISTEMIC,
    derive_claim_id,
    export_claim_card,
    import_record,
    normalize_quote,
    validate_cited_record,
    validate_record,
    verify_record,
)
from benchmark.amr.run import run_conformance, write_artifacts

ROOT = Path(__file__).resolve().parent
VECTORS = ROOT / "fixtures" / "conformance_vectors.json"


def signed_hash(quote: str) -> str:
    return "sha256:" + hashlib.sha256(normalize_quote(quote).encode()).hexdigest()


def card_fixture() -> dict:
    quote = "A current deployment uses the blue path."
    return {
        "entity_id": "claim-deploy",
        "claim": "the deployment uses the blue path",
        "epistemic_state": "inference",
        "provenance_class": "fact_derived",
        "confidence": 0.75,
        "verified": False,
        "times": {
            "valid_from_unix_ms": 1700000000000,
            "valid_to_unix_ms": None,
            "recorded_at_unix_ms": 1700000001000,
            "invalidated_at_unix_ms": None,
        },
        "scope": "workspace-a",
        "authority": {"agent_id": "agent-a", "visibility": "workspace"},
        "state": {
            "superseded": False,
            "superseded_by": None,
            "supersedes": None,
            "quarantined": False,
            "revoked": False,
            "tombstone": False,
        },
        "evidence": [
            {
                "entity_id": "source-runbook",
                "relationship": "evidence_for",
                "source_span": {
                    "source_ref": "sources/runbook.md",
                    "quote": quote,
                    "quote_hash": signed_hash(quote),
                },
            },
            {
                "entity_id": "claim-conflict",
                "relationship": "contradicts",
                "source_span": {
                    "source_ref": "sources/incident.md",
                    "quote": "An older note says the deployment uses the green path.",
                },
            },
        ],
        "links": [{"relationship": "contradicts", "target_id": "claim-conflict"}],
        "lifecycle": {"status": "active", "action": "serveable"},
    }


class NormalizationTests(unittest.TestCase):
    def test_quote_normalization_is_ascii_punctuation_and_idempotent(self):
        value = "the “price”   rose—sharply…\n"
        expected = 'the "price" rose-sharply...'
        self.assertEqual(normalize_quote(value), expected)
        self.assertEqual(normalize_quote(normalize_quote(value)), expected)

    def test_non_ascii_letters_are_preserved(self):
        self.assertEqual(normalize_quote("the café closed"), "the café closed")


class ExportTests(unittest.TestCase):
    def test_export_preserves_claim_ids_spans_hashes_links_and_vault_extensions(self):
        record = export_claim_card(card_fixture())
        validate_record(record)
        self.assertEqual(record["auditable_memory"], "0.1")
        self.assertEqual(record["ref"], "claim-deploy")
        self.assertEqual(record["epistemic"], "inference")
        self.assertEqual(record["backed_by"], ["source-runbook"])
        self.assertEqual(record["contradicts"], ["claim-conflict"])
        runbook_claim = next(claim for claim in record["claims"] if claim["source_id"] == "sources/runbook.md")
        self.assertEqual(runbook_claim["claim_id"], derive_claim_id("claim-deploy", card_fixture()["claim"]))
        self.assertEqual(runbook_claim["span"]["quote_hash"], signed_hash("A current deployment uses the blue path."))
        vault = record["extensions"]["vault"]
        self.assertEqual(vault["scope"], "workspace-a")
        self.assertEqual(vault["authority"]["agent_id"], "agent-a")
        self.assertEqual(vault["times"]["transaction_time"]["recorded_at_unix_ms"], 1700000001000)
        self.assertEqual(record["loss_report"]["lost_fields"], [])

    def test_absent_epistemic_is_not_fact(self):
        card = card_fixture()
        card.pop("epistemic_state")
        record = export_claim_card(card)
        self.assertNotIn("epistemic", record)
        self.assertIsNone(record["extensions"]["vault"]["epistemic_state"])
        validate_record(record)

    def test_unsupported_epistemic_and_required_loss_fail_closed(self):
        card = card_fixture()
        card["epistemic_state"] = "hypothesis"
        with self.assertRaisesRegex(AMRValidationError, "epistemic"):
            export_claim_card(card)
        card = card_fixture()
        card["lossy_required_fields"] = ["authority"]
        with self.assertRaisesRegex(AMRValidationError, "loss"):
            export_claim_card(card)

    def test_unknown_vault_fields_are_reported_without_copying_values(self):
        card = card_fixture()
        card["vault_only_note"] = "opaque internal value"
        record = export_claim_card(card)
        self.assertEqual(record["loss_report"]["lost_fields"], ["vault_only_note"])
        self.assertFalse(record["loss_report"]["lossless"])
        self.assertNotIn("opaque internal value", json.dumps(record, sort_keys=True))

    def test_export_rejects_benchmark_fields_at_any_depth(self):
        for field in ("raw_prompt", "question", "question_id", "question_type", "answer_session_ids", "evaluator_metadata", "hidden_label", "api_key", "api-key", "authorization", "password", "private_key", "private-key", "secret_key", "secret-key", "access-token", "model", "provider", "judge", "dataset", "split"):
            card = card_fixture()
            card[field] = "must not cross"
            with self.assertRaises(AMRValidationError):
                export_claim_card(card)
        card = card_fixture()
        card["evidence"][0]["api_key"] = "must not cross"
        with self.assertRaises(AMRValidationError):
            export_claim_card(card)

    def test_inferred_links_are_rejected_recursively(self):
        for location in ("evidence", "authority"):
            card = card_fixture()
            if location == "evidence":
                card[location][0]["inferred_links"] = [{"relationship": "backed_by", "target_id": "derived"}]
            else:
                card[location]["inferred_links"] = [{"relationship": "backed_by", "target_id": "derived"}]
            with self.assertRaisesRegex(AMRValidationError, "inferred"):
                export_claim_card(card)

    def test_unknown_nested_card_fields_fail_closed_instead_of_disappearing(self):
        cases = []
        card = card_fixture()
        card["evidence"][0]["unexpected"] = "not silently lost"
        cases.append(card)
        card = card_fixture()
        card["evidence"][0]["source_span"]["unexpected"] = "not silently lost"
        cases.append(card)
        card = card_fixture()
        card["links"][0]["unexpected"] = "not silently lost"
        cases.append(card)
        for card in cases:
            with self.assertRaisesRegex(AMRValidationError, "unsupported|unknown"):
                export_claim_card(card)

    def test_export_rejects_legacy_md5_while_verifier_keeps_compatibility(self):
        card = card_fixture()
        quote = card["evidence"][0]["source_span"]["quote"]
        card["evidence"][0]["source_span"]["quote_hash"] = "md5:" + hashlib.md5(normalize_quote(quote).encode()).hexdigest()
        with self.assertRaises(AMRValidationError):
            export_claim_card(card)

    def test_top_level_vault_lifecycle_and_authority_fields_are_extensions(self):
        card = card_fixture()
        card.pop("authority")
        card.update({
            "claim_card_version": 1,
            "agent_id": "agent-a",
            "visibility": "workspace",
            "revocation": {"status": "not_revoked"},
            "tombstone": False,
            "quarantine": False,
            "verified": True,
            "support_count": 7,
        })
        record = export_claim_card(card)
        vault = record["extensions"]["vault"]
        self.assertEqual(vault["authority"], {"agent_id": "agent-a", "visibility": "workspace"})
        self.assertEqual(vault["revocation"], {"status": "not_revoked"})
        self.assertFalse(vault["state"]["tombstone"])
        self.assertFalse(vault["state"]["quarantine"])
        self.assertTrue(vault["verified"])
        self.assertEqual(vault["support_count"], 7)
        self.assertEqual(record["loss_report"]["lost_fields"], [])

    def test_unmapped_nested_time_and_state_fields_are_retained(self):
        card = card_fixture()
        card["times"]["observed_at_unix_ms"] = 1700000002000
        card["state"]["review_queue"] = "manual"
        record = export_claim_card(card)
        vault = record["extensions"]["vault"]
        self.assertEqual(vault["times"]["unmapped_fields"]["times"]["observed_at_unix_ms"], 1700000002000)
        self.assertEqual(vault["state"]["unmapped_fields"]["review_queue"], "manual")
        self.assertTrue(record["loss_report"]["lossless"])

    def test_conflicting_aliases_and_malformed_nested_collections_fail_closed(self):
        card = card_fixture()
        card["valid_time"] = {"valid_from_unix_ms": 1700000000999}
        with self.assertRaisesRegex(AMRValidationError, "time"):
            export_claim_card(card)
        card = card_fixture()
        card["superseded_by"] = "newer"
        with self.assertRaisesRegex(AMRValidationError, "state"):
            export_claim_card(card)
        card = card_fixture()
        card["agent_id"] = "different-agent"
        with self.assertRaisesRegex(AMRValidationError, "authority"):
            export_claim_card(card)
        card = card_fixture()
        card["epistemic"] = "fact"
        with self.assertRaisesRegex(AMRValidationError, "epistemic"):
            export_claim_card(card)
        card = card_fixture()
        card["certainty"] = 0.25
        with self.assertRaisesRegex(AMRValidationError, "confidence"):
            export_claim_card(card)
        card = card_fixture()
        card["links"] = ["not-an-object"]
        with self.assertRaises(AMRValidationError):
            export_claim_card(card)

    def test_source_span_anchor_ids_are_preserved_verbatim(self):
        card = card_fixture()
        card["evidence"][0]["anchor_id"] = "p12#para-4::offset(88,151)"
        record = export_claim_card(card)
        runbook_claim = next(claim for claim in record["claims"] if claim["source_id"] == "sources/runbook.md")
        self.assertEqual(runbook_claim["anchor_id"], "p12#para-4::offset(88,151)")

    def test_deterministic_export_does_not_depend_on_evidence_input_order(self):
        first = export_claim_card(card_fixture())
        second_card = card_fixture()
        second_card["evidence"] = list(reversed(second_card["evidence"]))
        second = export_claim_card(second_card)
        self.assertEqual(first, second)


class VerificationTests(unittest.TestCase):
    def test_verification_distinguishes_four_citation_outcomes(self):
        quote = "The service stayed available."
        base = {
            "auditable_memory": "0.1",
            "ref": "claim-1",
            "sources": [{"ref": "src/a", "quote": quote, "quote_hash": signed_hash(quote)}],
        }
        self.assertEqual(verify_record(base, {"src/a": "prefix The service stayed available. suffix"})["status"], "ok")

        tampered = copy.deepcopy(base)
        tampered["sources"][0]["quote_hash"] = signed_hash("different")
        self.assertEqual(verify_record(tampered, {"src/a": quote})["status"], "anchor_tampered")

        drifted = copy.deepcopy(base)
        self.assertEqual(verify_record(drifted, {"src/a": "The service was unavailable."})["status"], "source_drifted")
        self.assertEqual(verify_record(base, {}).get("status"), "source_missing")

        legacy = {"auditable_memory": "0.1", "ref": "legacy", "sources": [{"ref": "src/a", "quote": "hello", "quote_hash": hashlib.md5(b"hello").hexdigest()}]}
        self.assertEqual(verify_record(legacy, {"src/a": "hello"})["status"], "ok")

    def test_verification_rejects_empty_citation_evidence(self):
        with self.assertRaisesRegex(AMRValidationError, "citation"):
            verify_record({"auditable_memory": "0.1", "ref": "empty"}, {})

    def test_quote_hash_absence_is_partial_not_tampered(self):
        record = {"auditable_memory": "0.1", "ref": "claim-1", "sources": [{"ref": "src/a", "quote": "hello"}]}
        result = verify_record(record, {"src/a": "hello"})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["partial"])
        self.assertNotEqual(result["status"], "anchor_tampered")

    def test_claim_span_hash_is_verified_even_when_source_anchor_is_valid(self):
        record = {
            "auditable_memory": "0.1",
            "ref": "claim-1",
            "sources": [{"ref": "src/a", "quote": "hello", "quote_hash": signed_hash("hello")}],
            "claims": [{"claim_id": "claim-span", "text": "hello", "source_id": "src/a", "span": {"quote": "hello", "quote_hash": signed_hash("different")}}],
        }
        result = verify_record(record, {"src/a": "hello"})
        self.assertEqual(result["status"], "anchor_tampered")

    def test_round_trip_export_verification_is_ok(self):
        record = export_claim_card(card_fixture())
        sources = {
            "sources/runbook.md": "A current deployment uses the blue path.",
            "sources/incident.md": "An older note says the deployment uses the green path.",
        }
        result = verify_record(record, sources)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["citations"]), 2)
        self.assertEqual({item["status"] for item in result["citations"]}, {"ok"})


class ContractTests(unittest.TestCase):
    def test_malformed_refs_epistemic_hash_and_required_claim_fields_fail_closed(self):
        cases = [
            {"auditable_memory": "0.1", "ref": "../escape"},
            {"auditable_memory": "0.1", "ref": "a", "epistemic": "hypothesis"},
            {"auditable_memory": "0.1", "ref": "a", "sources": [{"ref": "s", "quote": "x", "quote_hash": "crc32:deadbeef"}]},
            {"auditable_memory": "0.1", "ref": "a", "claims": [{"text": "x", "span": {"quote": "x"}}]},
        ]
        for record in cases:
            with self.assertRaises(AMRValidationError):
                validate_record(record)

    def test_extensions_reject_benchmark_and_provider_payloads(self):
        for field in ("raw_prompt", "model", "provider", "judge", "dataset", "split", "access-token", "private-key"):
            record = {"auditable_memory": "0.1", "ref": "safe", "extensions": {"vault": {field: "must not cross"}}}
            with self.assertRaises(AMRValidationError):
                validate_record(record)

    def test_duplicate_claim_bindings_fail_closed_but_claim_id_remains_optional(self):
        record = {
            "auditable_memory": "0.1",
            "ref": "safe",
            "claims": [
                {"claim_id": "same", "text": "x", "source_id": "src", "span": {"quote": "x"}},
                {"claim_id": "same", "text": "x", "source_id": "src", "span": {"quote": "x"}},
            ],
        }
        with self.assertRaises(AMRValidationError):
            validate_record(record)
        validate_record({"auditable_memory": "0.1", "ref": "optional-claim-id", "claims": [{"text": "x", "source_id": "src", "span": {"quote": "x"}}]})

    def test_refs_reject_whitespace_and_malformed_retained_evidence_ids(self):
        with self.assertRaises(AMRValidationError):
            validate_record({"auditable_memory": "0.1", "ref": " safe "})
        card = card_fixture()
        card["evidence"][0]["entity_id"] = "../escape"
        with self.assertRaises(AMRValidationError):
            export_claim_card(card)

    def test_cited_validation_rejects_empty_citations(self):
        with self.assertRaisesRegex(AMRValidationError, "citation"):
            validate_cited_record({"auditable_memory": "0.1", "ref": "empty"})

    def test_contradicts_is_non_resolving_and_queryable_as_a_typed_link(self):
        store = InMemoryAMRStore()
        a = {"auditable_memory": "0.1", "ref": "record-a", "contradicts": ["record-b"]}
        b = {"auditable_memory": "0.1", "ref": "record-b", "epistemic": "fact"}
        store.put(a)
        store.put(b)
        self.assertEqual(store.query_links("record-a", "contradicts"), ["record-b"])
        self.assertEqual(set(store.refs()), {"record-a", "record-b"})
        self.assertEqual(store.get("record-b")["epistemic"], "fact")

    def test_import_is_non_authoritative_and_never_promotes(self):
        record = {"auditable_memory": "0.1", "ref": "imported", "epistemic": "inference"}
        imported = import_record(record)
        self.assertFalse(imported["authority"]["authoritative"])
        self.assertTrue(imported["authority"]["promotion_required"])
        self.assertEqual(imported["record"]["epistemic"], "inference")

    def test_supported_epistemic_vocabulary_is_closed(self):
        self.assertEqual(SUPPORTED_EPISTEMIC, {"fact", "inference", "open_question", "unverified"})


class ConformanceTests(unittest.TestCase):
    def test_provider_free_conformance_vectors_all_pass(self):
        result = run_conformance(VECTORS)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["network_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["judge_calls"], 0)
        self.assertEqual(result["cases_total"], 54)
        self.assertEqual(result["cases_failed"], 0)
        self.assertEqual(result["levels"], ["marked", "linked", "cited"])

    def test_conformance_runner_does_not_trust_expected_flags(self):
        fixture = json.loads(VECTORS.read_text(encoding="utf-8"))
        mutations = {
            "l1-000d": lambda case: case.pop("corpus", None),
            "l2-012": lambda case: case.pop("attempted_card", None),
            "l3-012": lambda case: case.pop("record", None),
        }
        for case_id, mutate in mutations.items():
            broken = json.loads(json.dumps(fixture))
            for suite in broken["suites"].values():
                for case in suite:
                    if case.get("id") == case_id:
                        mutate(case)
            with tempfile.TemporaryDirectory(prefix="amr-vacuity-") as temp:
                path = Path(temp) / "vectors.json"
                path.write_text(json.dumps(broken), encoding="utf-8")
                result = run_conformance(path)
            self.assertEqual(result["status"], "blocked", case_id)
            self.assertGreater(result["cases_failed"], 0, case_id)

    def test_claim_span_anchor_collisions_are_not_silently_collapsed(self):
        card = card_fixture()
        card["evidence"] = [{
            "entity_id": "source-runbook",
            "relationship": "evidence_for",
            "claim_id": "claim-same",
            "claim_text": "same claim",
            "source_span": {"source_ref": "sources/runbook.md", "quote": "same quote", "anchor_id": "a1"},
        }, {
            "entity_id": "source-runbook",
            "relationship": "evidence_for",
            "claim_id": "claim-same",
            "claim_text": "same claim",
            "source_span": {"source_ref": "sources/runbook.md", "quote": "same quote", "anchor_id": "a2"},
        }]
        record = export_claim_card(card)
        self.assertEqual({claim["anchor_id"] for claim in record["claims"]}, {"a1", "a2"})

    def test_conflicting_claim_text_does_not_overwrite_a_same_anchor(self):
        card = card_fixture()
        card["evidence"] = [{
            "entity_id": "source-runbook",
            "relationship": "evidence_for",
            "claim_id": "claim-same",
            "claim_text": "first claim wording",
            "source_span": {"source_ref": "sources/runbook.md", "quote": "same quote", "anchor_id": "same-anchor"},
        }, {
            "entity_id": "source-runbook",
            "relationship": "evidence_for",
            "claim_id": "claim-same",
            "claim_text": "second claim wording",
            "source_span": {"source_ref": "sources/runbook.md", "quote": "same quote", "anchor_id": "same-anchor"},
        }]
        with self.assertRaisesRegex(AMRValidationError, "claim"):
            export_claim_card(card)

    def test_conformance_artifacts_are_hash_bound_and_repeatable(self):
        with tempfile.TemporaryDirectory(prefix="amr-artifacts-") as temp:
            first_dir = Path(temp) / "first"
            second_dir = Path(temp) / "second"
            first = write_artifacts(VECTORS, first_dir)
            second = write_artifacts(VECTORS, second_dir)
            for name in ("manifest.json", "conformance_report.json", "conformance_signature.txt", "artifact_inventory.json"):
                self.assertEqual((first_dir / name).read_bytes(), (second_dir / name).read_bytes(), name)
            report = json.loads((first_dir / "conformance_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["signature_sha256"], (first_dir / "conformance_signature.txt").read_text(encoding="ascii").strip())
            inventory = json.loads((first_dir / "artifact_inventory.json").read_text(encoding="utf-8"))
            for item in inventory["generated_artifacts"]:
                artifact = first_dir / item["path"]
                if not artifact.is_file():
                    artifact = ROOT / "fixtures" / item["path"]
                self.assertEqual(hashlib.sha256(artifact.read_bytes()).hexdigest(), item["sha256"])
            self.assertEqual(first["cases_failed"], 0)
            self.assertEqual(first["report_signature"], second["report_signature"])

    def test_conformance_result_is_deterministic(self):
        first = run_conformance(VECTORS)
        second = run_conformance(VECTORS)
        self.assertEqual(first, second)
        self.assertRegex(first["signature_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
