import hashlib
import unittest

from benchmark.hostile_memory_gauntlet.gauntlet.protocol import (
    ProtocolError,
    canonical_json,
    sanitize_public_projection,
    sha256_text,
    validate_case_bundle,
    validate_manifest,
)


def make_record(record_id="r1", text="Alex works at Acme.", scope="team-a"):
    return {
        "record_id": record_id,
        "memory_key": "profile.alex.employer",
        "scope": scope,
        "text": text,
        "source_ref": f"source://{record_id}",
        "record_digest": hashlib.sha256(text.encode()).hexdigest(),
        "actor": "alex",
        "trust": "authoritative",
        "valid_from": 1,
        "recorded_at": 1,
        "status": "active",
        "supersedes": [],
    }


def make_case(case_id="case-1"):
    return {
        "case_id": case_id,
        "category": "correction",
        "events": [{"type": "ingest", "record": make_record()}],
        "probes": [{
            "probe_id": "probe-1",
            "query": "where does Alex work",
            "scope": "team-a",
            "as_of": 2,
            "limit": 5,
            "expected": {
                "decision": "answer",
                "required_ids": ["r1"],
                "forbidden_ids": [],
                "required_scope": "team-a",
                "require_provenance": True,
                "max_context_words": 20,
            },
        }],
    }


class ProtocolTests(unittest.TestCase):
    def test_canonical_json_and_digest_are_stable(self):
        left = {"b": 2, "a": [1, True]}
        right = {"a": [1, True], "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(sha256_text(canonical_json(left)), sha256_text(canonical_json(right)))

    def test_case_requires_hash_bound_source_record(self):
        bundle = {"schema": "perseus-hostile-memory-gauntlet/cases/v1", "cases": [make_case()]}
        validate_case_bundle(bundle)
        broken = make_case()
        broken["events"][0]["record"]["record_digest"] = "not-a-digest"
        with self.assertRaises(ProtocolError):
            validate_case_bundle({"schema": bundle["schema"], "cases": [broken]})

    def test_admission_expectation_can_declare_equivalent_safe_dispositions(self):
        case = make_case()
        case["events"][0]["expected_status"] = ["quarantined", "rejected"]
        validate_case_bundle({"schema": "perseus-hostile-memory-gauntlet/cases/v1", "cases": [case]})

    def test_probe_evidence_ids_must_reference_case_records(self):
        case = make_case()
        case["probes"][0]["expected"]["required_ids"] = ["missing-record"]
        with self.assertRaises(ProtocolError):
            validate_case_bundle({"schema": "perseus-hostile-memory-gauntlet/cases/v1", "cases": [case]})

    def test_probe_budget_must_fit_required_evidence(self):
        case = make_case()
        case["probes"][0]["expected"]["max_context_words"] = 0
        with self.assertRaises(ProtocolError):
            validate_case_bundle({"schema": "perseus-hostile-memory-gauntlet/cases/v1", "cases": [case]})

    def test_probe_limit_must_fit_all_required_evidence(self):
        case = make_case()
        case["events"].append({"type": "ingest", "record": make_record("r2", "Taylor works at Beta.")})
        case["probes"][0]["expected"]["required_ids"] = ["r1", "r2"]
        case["probes"][0]["limit"] = 1
        with self.assertRaises(ProtocolError):
            validate_case_bundle({"schema": "perseus-hostile-memory-gauntlet/cases/v1", "cases": [case]})

    def test_manifest_rejects_duplicate_case_ids(self):
        manifest = {
            "schema": "perseus-hostile-memory-gauntlet/manifest/v1",
            "suite_id": "public-control-v1",
            "case_ids": ["case-1", "case-1"],
            "required_categories": ["correction"],
            "config": {"max_cases": 30},
        }
        with self.assertRaises(ProtocolError):
            validate_manifest(manifest)

    def test_public_projection_drops_raw_content_fields(self):
        raw = {
            "schema": "perseus-hostile-memory-gauntlet/run-return/v1",
            "case_results": [{
                "case_id": "case-1",
                "query": "private question",
                "record_body": "private memory",
                "passed": True,
            }],
            "metrics": {"probe_pass_rate": 1.0},
        }
        clean = sanitize_public_projection(raw)
        self.assertNotIn("query", clean["case_results"][0])
        self.assertNotIn("record_body", clean["case_results"][0])
        self.assertEqual(clean["case_results"][0]["case_id"], "case-1")

    def test_public_projection_rejects_secret_markers(self):
        with self.assertRaises(ProtocolError):
            sanitize_public_projection({"api_key": "never persist this"})


if __name__ == "__main__":
    unittest.main()
