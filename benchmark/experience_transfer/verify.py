#!/usr/bin/env python3
"""Independent readiness gate for the provider-free benchmark package."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

from .adapters import GovernedVaultAdapter
from .common import ContractError, canonical_digest, public_report_signature, validate_corpus, validate_public_report, validate_shared_views
from .generate_corpus import build_corpus
from .reference_workflow.implementation import build_receipt

ROOT = Path(__file__).resolve().parent
CORPUS_PATH = ROOT / "corpus/corpus.json"
CORPUS_MANIFEST_PATH = ROOT / "corpus/manifest.json"
SHARED_VIEWS_PATH = ROOT / "corpus/shared_agent_views.json"
LABEL_COMMITMENTS_PATH = ROOT / "corpus/label_commitments.json"
REPORT_DIR = ROOT / "reports/provider_free"
MANIFEST_PATH = REPORT_DIR / "manifest.json"
REPORT_PATH = REPORT_DIR / "public_report.json"
ACCEPTANCE_PATH = REPORT_DIR / "acceptance_report.json"
WORKFLOW_PATH = ROOT / "reference_workflow/receipts.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_tamper_probes(corpus: dict) -> dict[str, bool]:
    probes: dict[str, bool] = {}
    broken = copy.deepcopy(corpus)
    broken["cases"][0]["experience"]["source"]["commitment"] = "0" * 64
    try:
        validate_corpus(broken)
    except ContractError:
        probes["tampered_source_rejected"] = True
    else:
        probes["tampered_source_rejected"] = False
    broken = copy.deepcopy(corpus)
    broken["cases"][0]["transition_chain"][-1]["authorized"] = False
    try:
        validate_corpus(broken)
    except ContractError:
        probes["unauthorized_transition_rejected"] = True
    else:
        probes["unauthorized_transition_rejected"] = False
    broken_view = copy.deepcopy(corpus["cases"][0]["agent_view"])
    broken_view["world"]["state_hash"] = "0" * 64
    try:
        GovernedVaultAdapter().evaluate(broken_view)
    except ContractError:
        probes["tampered_agent_view_rejected"] = True
    else:
        probes["tampered_agent_view_rejected"] = False
    return probes


def main() -> int:
    checks: dict[str, bool] = {}
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    checks["corpus_valid"] = True
    generated = json.dumps(build_corpus(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    checks["corpus_regeneration_matches"] = generated.encode("utf-8") == CORPUS_PATH.read_bytes()
    manifest = json.loads(CORPUS_MANIFEST_PATH.read_text(encoding="utf-8"))
    checks["corpus_manifest_file_hash"] = manifest["corpus_sha256"] == sha(CORPUS_PATH)
    checks["corpus_manifest_canonical_hash"] = manifest["corpus_canonical_sha256"] == canonical_digest(corpus)
    checks["corpus_manifest_generator_hash"] = manifest["generator_sha256"] == sha(ROOT / "generate_corpus.py")
    shared = json.loads(SHARED_VIEWS_PATH.read_text(encoding="utf-8"))
    validate_shared_views(shared)
    label_commitments = json.loads(LABEL_COMMITMENTS_PATH.read_text(encoding="utf-8"))
    checks["shared_views_valid"] = True
    checks["shared_views_count"] = len(shared["cases"]) == len(corpus["cases"])
    checks["shared_views_are_label_blind"] = "expected_decision_class" not in json.dumps(shared, sort_keys=True) and "negative_control" not in json.dumps(shared, sort_keys=True)
    checks["label_commitments_separate"] = "expected_decision_class" not in json.dumps(label_commitments, sort_keys=True) and len(label_commitments["cases"]) == len(corpus["cases"])
    checks["shared_views_file_hash"] = manifest.get("shared_views_file_sha256") == sha(SHARED_VIEWS_PATH)
    checks["label_commitments_file_hash"] = manifest.get("label_commitments_file_sha256") == sha(LABEL_COMMITMENTS_PATH)
    checks.update(check_tamper_probes(corpus))
    run_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    acceptance = json.loads(ACCEPTANCE_PATH.read_text(encoding="utf-8"))
    validate_public_report(report)
    checks["report_valid"] = True
    checks["report_signature_valid"] = report["report_signature_sha256"] == public_report_signature(report)
    checks["report_corpus_binding"] = report["corpus_sha256"] == sha(CORPUS_PATH)
    checks["report_manifest_binding"] = report["manifest_sha256"] == sha(MANIFEST_PATH)
    checks["report_provider_free"] = report["provider_calls"] == 0 and report["judge_calls"] == 0
    checks["report_has_no_expected_labels"] = "expected_decision_class" not in json.dumps(report, sort_keys=True)
    checks["report_has_no_raw_payload_markers"] = all(marker not in json.dumps(report, sort_keys=True).lower() for marker in ("payload-must-not-land", "bearer ", "sk-", "customer-secret"))
    checks["manifest_corpus_binding"] = run_manifest["corpus_file_sha256"] == sha(CORPUS_PATH)
    checks["manifest_provider_free"] = run_manifest["provider_free"] is True and run_manifest["network_calls"] == 0
    checks["manifest_shared_views_binding"] = run_manifest.get("shared_views_file_sha256") == sha(SHARED_VIEWS_PATH) and run_manifest.get("label_commitments_file_sha256") == sha(LABEL_COMMITMENTS_PATH)
    checks["acceptance_status_ready"] = acceptance["status"] == "ready_for_provider_free_review"
    checks["acceptance_report_binding"] = acceptance["report_sha256"] == sha(REPORT_PATH)
    checks["acceptance_manifest_binding"] = acceptance["manifest_sha256"] == sha(MANIFEST_PATH)
    checks["acceptance_report_signature"] = acceptance["report_signature_sha256"] == report["report_signature_sha256"]
    checks["negative_control_count"] = sum(case["controls"]["negative_control"] for case in corpus["cases"]) == 15
    checks["three_adapters_x_24_cases"] = len(report["case_results"]) == 72
    governed = [row for row in report["case_results"] if row["adapter"] == "perseus_vault_governed"]
    checks["governed_synthetic_agreement"] = len(governed) == 24 and all(row["decision_match"] for row in governed)
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    checks["workflow_four_receipts"] = len(workflow["receipts"]) == 4
    checks["workflow_provider_free"] = workflow["provider_calls"] == 0 and workflow["external_side_effects"] == 0
    workflow_ok = True
    for case in corpus["cases"]:
        if case["case_id"] in {item["case_id"] for item in workflow["receipts"]}:
            expected = build_receipt(case)
            actual = next(item for item in workflow["receipts"] if item["case_id"] == case["case_id"])
            workflow_ok = workflow_ok and expected == actual
    checks["workflow_receipts_recompute"] = workflow_ok
    # Public report status is ready even though provider-backed efficacy is
    # intentionally unmeasured; all structural checks must be true.
    passed = all(checks.values())
    readiness = {
        "schema": "verified-experience-transfer-readiness/v1",
        "status": "ready_for_provider_free_review" if passed else "correction_required",
        "provider_calls": 0,
        "judge_calls": 0,
        "network_calls": 0,
        "pairs": len(corpus["cases"]),
        "public_case_results": len(report["case_results"]),
        "external_adapter": "not_measured",
        "checks": checks,
        "artifact_hashes": {
            "corpus": sha(CORPUS_PATH),
            "corpus_manifest": sha(CORPUS_MANIFEST_PATH),
            "shared_agent_views": sha(SHARED_VIEWS_PATH),
            "label_commitments": sha(LABEL_COMMITMENTS_PATH),
            "run_manifest": sha(MANIFEST_PATH),
            "public_report": sha(REPORT_PATH),
            "acceptance_report": sha(ACCEPTANCE_PATH),
            "workflow_receipts": sha(WORKFLOW_PATH),
        },
        "claim_boundary": {
            "supported": ["provider-free corpus, contract, integrity, leakage, and receipt readiness"],
            "not_supported": ["agent-task efficacy", "customer/production efficacy", "external implementation comparison", "universal superiority"],
        },
    }
    output_path = REPORT_DIR / "readiness_report.json"
    output_path.write_text(json.dumps(readiness, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(output_path), "status": readiness["status"], "checks": len(checks), "failed": [key for key, value in checks.items() if not value]}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
