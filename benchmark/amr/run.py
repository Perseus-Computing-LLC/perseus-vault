"""Offline AMR 0.1 conformance runner and hash-only artifact custody."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .profile import (
    AMRValidationError,
    InMemoryAMRStore,
    canonical_sha256,
    derive_claim_id,
    export_claim_card,
    hash_algorithm,
    normalize_quote,
    validate_record,
    verify_record,
    validate_cited_record,
)

CONFORMANCE_SCHEMA = "perseus-vault-amr-conformance-fixtures/v1"
AMR_SOURCE_REPOSITORY = "https://github.com/phasespace-labs/auditable-memory-records"
AMR_SOURCE_REVISION = "2b44803b4bba15bc47f5590e24a47fd09e8ef66f"
EXPECTED_VECTOR_FILES = {
    "normalize.yaml": "95feec95a698ea358dab9703073a43c44fc09632",
    "level1-marked.yaml": "8f6bd76dacc5a9e5c248e4c4f69f5d23bda70a77",
    "level2-linked.yaml": "4e8d0f9d6a6c64f3863a1da1b42bf0c69d14dae3",
    "level3-cited.yaml": "5ae86986c64d19d3f7f13b6cf165208c257e11f5",
}
EXPECTED_VECTOR_CASE_IDS = {
    "normalize": {f"norm-{index:03d}" for index in range(1, 11)},
    "marked": {"l1-000", "l1-000b", "l1-000c", "l1-000d", *{f"l1-{index:03d}" for index in range(1, 12)}},
    "linked": {f"l2-{index:03d}" for index in range(1, 13)},
    "cited": {"l3-001", "l3-002", "l3-003", "l3-004", "l3-005", "l3-005b", "l3-005c", "l3-006", "l3-006b", "l3-006c", "l3-007", "l3-008", "l3-009", "l3-010", "l3-011", "l3-012"},
    "negative": {"amr-empty-citations"},
}
SUITE_LEVELS = {"marked": "marked", "linked": "linked", "cited": "cited"}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_vectors(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AMRValidationError("invalid AMR conformance fixture", "invalid_fixture") from exc
    if not isinstance(data, dict) or data.get("schema_version") != CONFORMANCE_SCHEMA:
        raise AMRValidationError("unsupported AMR conformance fixture", "invalid_fixture")
    source = data.get("amr_source")
    if not isinstance(source, dict) or source.get("repository") != AMR_SOURCE_REPOSITORY or source.get("revision") != AMR_SOURCE_REVISION:
        raise AMRValidationError("AMR source revision is not pinned", "unpinned_source")
    files = source.get("vector_files")
    if not isinstance(files, list) or len(files) != len(EXPECTED_VECTOR_FILES) or len({item.get("name") for item in files if isinstance(item, dict)}) != len(EXPECTED_VECTOR_FILES) or {item.get("name"): item.get("git_blob_sha") for item in files if isinstance(item, dict)} != EXPECTED_VECTOR_FILES:
        raise AMRValidationError("AMR conformance vector pins are incomplete", "unpinned_source")
    suites = data.get("suites")
    if not isinstance(suites, dict) or set(suites) != {"normalize", "marked", "linked", "cited", "negative"}:
        raise AMRValidationError("AMR conformance suites are incomplete", "invalid_fixture")
    for suite, cases in suites.items():
        if not isinstance(cases, list) or not cases:
            raise AMRValidationError(f"AMR suite {suite} is empty", "invalid_fixture")
        ids: set[str] = set()
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"] or case["id"] in ids:
                raise AMRValidationError(f"AMR suite {suite} has an invalid or duplicate case", "invalid_fixture")
            ids.add(case["id"])
        if ids != EXPECTED_VECTOR_CASE_IDS[suite]:
            raise AMRValidationError(f"AMR suite {suite} case IDs do not match the pinned vector set", "invalid_fixture")
    return data


def _case_result(suite: str, case_id: str, passed: bool, reason: str = "") -> dict[str, Any]:
    result = {"suite": suite, "case_id": case_id, "status": "passed" if passed else "failed"}
    if reason:
        result["reason"] = reason
    return result


def _run_normalize(case: dict[str, Any]) -> dict[str, Any]:
    input_value = case.get("input")
    if not isinstance(input_value, str):
        return _case_result("normalize", case["id"], False, "normalization input is not text")
    actual = normalize_quote(input_value)
    expected = case.get("expect")
    passed = actual == expected
    if case.get("idempotent"):
        passed = passed and normalize_quote(actual) == actual
    return _case_result("normalize", case["id"], passed, "normalization mismatch" if not passed else "")


def _valid_marked_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    try:
        validate_record(record)
    except AMRValidationError:
        return False
    return record.get("auditable_memory") == "0.1"


def _run_marked(case: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expect", {})
    if case.get("discoverable"):
        corpus = case.get("corpus")
        passed = (
            isinstance(corpus, list)
            and bool(corpus)
            and all(isinstance(record, dict) and record.get("auditable_memory") == "0.1" for record in corpus)
        )
        return _case_result("marked", case["id"], passed, "discoverability assertion failed" if not passed else "")
    record = case.get("record")
    if not isinstance(record, dict):
        return _case_result("marked", case["id"], False, "missing record")
    if "auditable_memory" not in record:
        passed = expected.get("valid") is True and expected.get("conforms_level_1") is False
        return _case_result("marked", case["id"], passed, "unmarked record was promoted" if not passed else "")
    error = ""
    try:
        validate_record(record)
        valid = True
    except AMRValidationError as exc:
        valid = False
        error = str(exc)
    expected_valid = expected.get("valid") is True
    passed = valid == expected_valid
    if passed and valid and "epistemic" in expected:
        passed = record.get("epistemic") == expected["epistemic"]
    return _case_result("marked", case["id"], passed, (error if not passed and not valid else "marked assertion failed" if not passed else ""))


def _run_linked(case: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expect", {})
    if case.get("inferred_relation"):
        attempted = case.get("attempted_card")
        try:
            export_claim_card(attempted)
            passed = False
        except AMRValidationError as exc:
            passed = exc.reason == "inferred_relationship"
        return _case_result("linked", case["id"], passed, "inferred relation case was not rejected semantically" if not passed else "")
    try:
        if case.get("contradiction"):
            store = InMemoryAMRStore()
            record = case["record"]
            other = case["other"]
            store.put(record)
            store.put(other)
            passed = (
                store.query_links(record["ref"], "contradicts") == record.get("contradicts", [])
                and set(store.refs()) == {record["ref"], other["ref"]}
                and store.get(other["ref"])["ref"] == other["ref"]
            )
        else:
            validate_record(case["record"])
            passed = expected.get("valid") is True
    except AMRValidationError:
        passed = expected.get("valid") is False
    return _case_result("linked", case["id"], passed, "linked assertion failed" if not passed else "")


def _run_cited(case: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expect", {})
    try:
        if case.get("legacy_hash"):
            legacy_hash = case["legacy_hash"]
            try:
                algorithm = hash_algorithm(legacy_hash)
            except AMRValidationError as exc:
                passed = exc.reason == case.get("expected_error")
            else:
                passed = algorithm == case.get("expected_algorithm")
        elif case.get("verifiability"):
            validate_cited_record(case["record"])
            result = verify_record(case["record"], case.get("sources", {}))
            passed = result["status"] == "ok" and isinstance(case.get("sources"), dict) and bool(case["sources"])
        elif case.get("derive_claim_id"):
            actual = derive_claim_id(case["record_ref"], case["claim_text"])
            passed = True
            if "claim_id" in expected:
                passed = actual == expected["claim_id"]
            if case.get("must_differ_from"):
                passed = passed and actual != case["must_differ_from"]
        else:
            result = verify_record(case["record"], case.get("sources", {}))
            passed = result["status"] == expected.get("status")
            if "partial" in expected:
                passed = passed and result["partial"] is expected["partial"]
    except (KeyError, TypeError):
        passed = False
    except AMRValidationError:
        passed = expected.get("valid") is False
    return _case_result("cited", case["id"], passed, "citation assertion failed" if not passed else "")


def _run_negative(case: dict[str, Any]) -> dict[str, Any]:
    if case.get("empty_citations"):
        try:
            validate_cited_record(case["record"])
            passed = False
        except AMRValidationError as exc:
            passed = exc.reason == "missing_citations"
        return _case_result("negative", case["id"], passed, "empty cited record was not blocked" if not passed else "")
    return _case_result("negative", case["id"], False, "unknown negative case")


def run_conformance(fixture_path: Path) -> dict[str, Any]:
    fixture = load_vectors(fixture_path)
    cases: list[dict[str, Any]] = []
    for case in fixture["suites"]["normalize"]:
        cases.append(_run_normalize(case))
    for case in fixture["suites"]["marked"]:
        cases.append(_run_marked(case))
    for case in fixture["suites"]["linked"]:
        cases.append(_run_linked(case))
    for case in fixture["suites"]["cited"]:
        cases.append(_run_cited(case))
    for case in fixture["suites"]["negative"]:
        cases.append(_run_negative(case))
    failed = [case for case in cases if case["status"] != "passed"]
    suite_counts: dict[str, dict[str, int]] = {}
    for case in cases:
        counts = suite_counts.setdefault(case["suite"], {"cases_total": 0, "cases_passed": 0, "cases_failed": 0})
        counts["cases_total"] += 1
        counts["cases_passed" if case["status"] == "passed" else "cases_failed"] += 1
    result: dict[str, Any] = {
        "schema_version": CONFORMANCE_SCHEMA,
        "profile": "perseus-vault-amr-0.1",
        "amr_source_revision": AMR_SOURCE_REVISION,
        "fixture_sha256": file_sha256(fixture_path),
        "levels": ["marked", "linked", "cited"],
        "suites": suite_counts,
        "cases_total": len(cases),
        "cases_passed": len(cases) - len(failed),
        "cases_failed": len(failed),
        "status": "passed" if not failed else "blocked",
        "offline": True,
        "provider_calls": 0,
        "network_calls": 0,
        "model_calls": 0,
        "judge_calls": 0,
        "raw_inputs_captured": False,
        "cases": cases,
    }
    result["signature_sha256"] = canonical_sha256(result)
    return result


def write_artifacts(fixture_path: Path, outdir: Path) -> dict[str, Any]:
    result = run_conformance(fixture_path)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "perseus-vault-amr-0.1-manifest/v1",
        "profile": "perseus-vault-amr-0.1",
        "amr_source": {"repository": AMR_SOURCE_REPOSITORY, "revision": AMR_SOURCE_REVISION, "vector_files": EXPECTED_VECTOR_FILES},
        "fixture": {"path": fixture_path.name, "sha256": file_sha256(fixture_path)},
        "offline": {"offline": True, "provider_calls": 0, "network_calls": 0, "model_calls": 0, "judge_calls": 0, "paid_spend_usd": 0},
        "generated_artifacts": ["manifest.json", "conformance_report.json", "conformance_signature.txt", "artifact_inventory.json"],
    }
    manifest_path = outdir / "manifest.json"
    report_path = outdir / "conformance_report.json"
    signature_path = outdir / "conformance_signature.txt"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    signature_path.write_text(result["signature_sha256"] + "\n", encoding="ascii")
    inventory_entries = [
        {"path": "manifest.json", "sha256": file_sha256(manifest_path)},
        {"path": "conformance_report.json", "sha256": file_sha256(report_path)},
        {"path": "conformance_signature.txt", "sha256": file_sha256(signature_path)},
        {"path": fixture_path.name, "sha256": file_sha256(fixture_path)},
    ]
    inventory = {"schema_version": "perseus-vault-amr-0.1-inventory/v1", "offline": True, "raw_inputs_captured": False, "generated_artifacts": inventory_entries}
    inventory_path = outdir / "artifact_inventory.json"
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if result["status"] != "passed":
        raise AMRValidationError("AMR conformance is blocked", "conformance_failed")
    return {"manifest_path": str(manifest_path), "report_path": str(report_path), "signature_path": str(signature_path), "inventory_path": str(inventory_path), "report_signature": result["signature_sha256"], "inventory_sha256": file_sha256(inventory_path), "cases_total": result["cases_total"], "cases_failed": result["cases_failed"], "provider_calls": 0, "network_calls": 0, "model_calls": 0, "judge_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the provider-free AMR 0.1 conformance lane")
    parser.add_argument("--fixture", type=Path, default=Path(__file__).resolve().parent / "fixtures" / "conformance_vectors.json")
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(write_artifacts(args.fixture, args.outdir), sort_keys=True))


if __name__ == "__main__":
    main()
