"""Validate the versioned cross-product adapter conformance contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "perseus-vault-integration-conformance/v1"
REQUIRED_CASES = frozenset(
    {
        "remember_idempotent",
        "workspace_isolation",
        "empty_recall",
        "timeout_or_backend_error",
        "forget_preserves_history",
        "provenance_projection",
    }
)
STATUSES = frozenset({"pass", "fail", "degraded", "skip"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = frozenset(
    {"prompt", "body", "raw", "content", "secret", "api_key", "token", "password"}
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _check_digest(value: Any, field: str) -> None:
    _require(isinstance(value, str) and _SHA256.fullmatch(value) is not None, f"{field} must be a lowercase SHA-256 digest")


def _validate_sanitized(value: Any, path: str = "report") -> None:
    """Reject forbidden material recursively, including nested evidence maps."""
    if isinstance(value, dict):
        for key, child in value.items():
            _require(isinstance(key, str), f"{path} keys must be strings")
            _require(
                key.lower() not in _FORBIDDEN_KEYS,
                f"{path}.{key} contains raw or secret material",
            )
            _validate_sanitized(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_sanitized(child, f"{path}[{index}]")


def validate_contract_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(fixture, dict), "fixture must be an object")
    _require(fixture.get("contract_version") == CONTRACT_VERSION, "unsupported contract_version")
    for field in ("vault_schema_version", "purpose"):
        _require(isinstance(fixture.get(field), str) and fixture[field], f"fixture needs {field}")
    report_requirements = fixture.get("report_requirements")
    _require(isinstance(report_requirements, dict), "fixture needs report_requirements")
    _require(isinstance(report_requirements.get("required_fields"), list), "report_requirements.required_fields must be a list")
    _require(isinstance(report_requirements.get("result_fields"), list), "report_requirements.result_fields must be a list")
    _require(isinstance(report_requirements.get("statuses"), list), "report_requirements.statuses must be a list")
    cases = fixture.get("cases")
    _require(isinstance(cases, list), "cases must be a list")
    ids = []
    for case in cases:
        _require(isinstance(case, dict), "each case must be an object")
        case_id = case.get("id")
        _require(isinstance(case_id, str) and case_id, "each case needs a string id")
        ids.append(case_id)
        _require(isinstance(case.get("operation"), str) and case["operation"], f"case {case_id} needs an operation")
        expected = case.get("expected")
        _require(isinstance(expected, list) and expected, f"case {case_id} needs expected assertions")
        _require(all(isinstance(assertion, str) and assertion for assertion in expected), f"case {case_id} expected assertions must be strings")
    _require(len(ids) == len(set(ids)), "case ids must be unique")
    _require(REQUIRED_CASES.issubset(ids), "fixture is missing a required conformance case")
    return fixture


def validate_report(report: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(report, dict), "report must be an object")
    _require(report.get("contract_version") == CONTRACT_VERSION, "unsupported contract_version")
    for field in ("adapter", "adapter_version", "vault_version"):
        _require(isinstance(report.get(field), str) and report[field], f"report needs {field}")
    results = report.get("results")
    _require(isinstance(results, list) and results, "report needs results")
    case_ids: list[str] = []
    for result in results:
        _require(isinstance(result, dict), "each result must be an object")
        case_id = result.get("case_id")
        _require(isinstance(case_id, str) and case_id, "result case_id must be a non-empty string")
        _require(case_id not in case_ids, "result case_id must be unique")
        _require(case_id in REQUIRED_CASES, f"unknown case_id: {case_id}")
        case_ids.append(case_id)
        status = result.get("status")
        _require(isinstance(status, str) and status in STATUSES, f"invalid status for {case_id}")
        _check_digest(result.get("evidence_digest"), "evidence_digest")
        if status != "pass":
            _require(isinstance(result.get("explanation"), str) and result["explanation"], f"non-pass result {case_id} needs an explanation")
    _require(set(case_ids) == REQUIRED_CASES, "report must include all required cases exactly once")
    _validate_sanitized(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    validate_contract_fixture(json.loads(args.fixture.read_text()))
    if args.report:
        validate_report(json.loads(args.report.read_text()))
    print(f"validated {CONTRACT_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
