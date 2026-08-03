import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.validate_integration_conformance import (
    CONTRACT_VERSION,
    validate_contract_fixture,
    validate_report,
)


ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "integration_conformance_v1.json"


def test_published_fixture_has_all_required_cases():
    fixture = json.loads(FIXTURE.read_text())
    assert validate_contract_fixture(fixture) is fixture
    assert fixture["contract_version"] == CONTRACT_VERSION
    assert {case["id"] for case in fixture["cases"]} >= {
        "remember_idempotent",
        "workspace_isolation",
        "empty_recall",
        "timeout_or_backend_error",
        "forget_preserves_history",
        "provenance_projection",
    }


def test_report_requires_sanitized_case_results():
    report = {
        "contract_version": CONTRACT_VERSION,
        "adapter": "example",
        "adapter_version": "0.1.0",
        "vault_version": "2.22.0",
        "results": [
            {"case_id": case_id, "status": "pass", "evidence_digest": "a" * 64}
            for case_id in (
                "remember_idempotent",
                "workspace_isolation",
                "empty_recall",
                "timeout_or_backend_error",
                "forget_preserves_history",
                "provenance_projection",
            )
        ],
    }
    assert validate_report(report)["adapter"] == "example"

    invalid = dict(report)
    invalid["results"] = [
        dict(report["results"][0], evidence_digest=None),
        *report["results"][1:],
    ]
    with pytest.raises(ValueError, match="evidence_digest"):
        validate_report(invalid)


def test_report_requires_all_cases_and_rejects_nested_raw_material():
    fixture = json.loads(FIXTURE.read_text())
    report = {
        "contract_version": CONTRACT_VERSION,
        "adapter": "example",
        "adapter_version": "0.1.0",
        "vault_version": "2.22.0",
        "results": [
            {"case_id": case["id"], "status": "pass", "evidence_digest": "b" * 64}
            for case in fixture["cases"]
        ],
    }
    incomplete = dict(report)
    incomplete["results"] = report["results"][:-1]
    with pytest.raises(ValueError, match="all required cases"):
        validate_report(incomplete)

    nested = dict(report)
    nested["results"] = [dict(item) for item in report["results"]]
    nested["results"][0]["evidence"] = {"content": "must not be reported"}
    with pytest.raises(ValueError, match="raw or secret"):
        validate_report(nested)


def test_malformed_types_are_validation_errors_not_type_errors():
    fixture = json.loads(FIXTURE.read_text())
    malformed_fixture = dict(fixture)
    malformed_fixture["cases"] = [dict(fixture["cases"][0], id=["not-a-string"])]
    with pytest.raises(ValueError, match="id"):
        validate_contract_fixture(malformed_fixture)

    malformed_report = {
        "contract_version": CONTRACT_VERSION,
        "adapter": "example",
        "adapter_version": "0.1.0",
        "vault_version": "2.22.0",
        "results": [{"case_id": [], "status": [], "evidence_digest": "c" * 64}],
    }
    with pytest.raises(ValueError, match="case_id"):
        validate_report(malformed_report)
