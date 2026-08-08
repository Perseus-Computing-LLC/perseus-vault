#!/usr/bin/env python3
"""Build a blocking release decision scorecard from a quality report."""

import argparse
import json
import math
import sys
from pathlib import Path

from run import LEGACY_REQUIRED_CATEGORIES, V0_REQUIRED_CATEGORIES, V1_REQUIRED_CATEGORIES
from benchmark.package.common.artifacts import validate_report


# Unit-level scorecard tests exercise the scoring logic with compact legacy
# fixtures. Full publication reports are validated by the runner boundary.
_SCORECARD_FIXTURE_KEYS = {"cases", "passed", "checks_passed", "checks_total", "accuracy"}


def _is_publishable_report(report):
    return all(key in report for key in (
        "schema_version", "benchmark_id", "suite_version", "control_profile_sha256",
        "run_fingerprint_sha256", "result_signature_sha256", "binary_sha256",
        "dataset_sha256", "harness_commit", "claims_sha256", "public_evidence",
        "raw_inputs_captured", "capabilities", "cases", "metrics", "not_measured", "excluded",
        "claim_ids", "negative_claim_ids", "benchmark", "dataset", "harness_version",
        "passed", "checks_passed", "checks_total", "accuracy", "required_categories",
        "metric_rates", "signature_sha256",
    ))

MINIMUM_ACCURACY = 1.0
REQUIRED_APPROVER = "maintainer"
REQUIRED_METRIC_RATES = {
    "validity_rate",
    "stale_recall_rate",
    "scope_invalid_recall_rate",
    "provenance_completeness",
    "replay_fidelity",
    "mutation_supersession_rate",
    "compaction_projection_rate",
    "action_grounding_rate",
}
V1_REQUIRED_METRIC_RATES = {
    "recall_outcome_rate",
    "admission_rate",
    "prompt_safety_rate",
    "identity_ambiguity_rate",
}


def _unavailable_categories(report):
    categories = set(report.get("unavailable_categories", []))
    cases = set(report.get("unavailable_cases", []))
    for case in report.get("cases", []):
        if case.get("status") == "unavailable":
            categories.add(case.get("category"))
            if case.get("id"):
                cases.add(case["id"])
    categories.discard(None)
    return sorted(categories), sorted(item for item in cases if item)


def _blocking_capabilities(report):
    capabilities = report.get("capabilities", {})
    return sorted(
        name
        for name, state in capabilities.items()
        if not isinstance(state, dict) or state.get("status") != "available"
    )


def _strict_count(value):
    """Accept JSON integers only; never truncate floats or numeric strings."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strict_number(value):
    """Accept finite JSON numbers only; reject booleans and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _is_v0_report(report):
    categories = {case.get("category") for case in report.get("cases", [])}
    return (
        report.get("dataset") == "perseus-vault-memory-quality-v0"
        or report.get("harness_version") == "perseus-vault-memory-quality/v0"
        or categories.intersection(set(V0_REQUIRED_CATEGORIES) - set(LEGACY_REQUIRED_CATEGORIES))
    )


def _is_v1_report(report):
    categories = {case.get("category") for case in report.get("cases", [])}
    return report.get("harness_version") == "perseus-vault-memory-quality/v1" or bool(
        categories.intersection(set(V1_REQUIRED_CATEGORIES) - set(V0_REQUIRED_CATEGORIES))
    )


def _required_categories(report):
    """Select the contract from report identity, not self-declared omissions."""
    categories = {case.get("category") for case in report.get("cases", [])}
    dataset = report.get("dataset")
    harness_version = report.get("harness_version", "")
    if _is_v1_report(report):
        return set(V1_REQUIRED_CATEGORIES)
    if (
        dataset == "perseus-vault-memory-quality-v0"
        or harness_version == "perseus-vault-memory-quality/v0"
        or categories.intersection(set(V0_REQUIRED_CATEGORIES) - set(LEGACY_REQUIRED_CATEGORIES))
    ):
        return set(V0_REQUIRED_CATEGORIES)
    return set(LEGACY_REQUIRED_CATEGORIES)


def build_scorecard(report):
    if not _is_publishable_report(report):
        return {"scorecard_version": "perseus-vault-memory-quality-scorecard/v2", "verdict": "blocked", "blocking": True, "reason": "incomplete_publication_envelope"}
    try:
        validate_report(report)
    except (TypeError, ValueError, KeyError, OverflowError):
        return {"scorecard_version": "perseus-vault-memory-quality-scorecard/v2", "verdict": "blocked", "blocking": True, "reason": "report_validation_failed"}
    if report.get("harness_version") == "perseus-vault-memory-quality/v1":
        required_case_fields = {"id", "category", "status", "checks", "evidence"}
        if any(not required_case_fields.issubset(case) for case in report.get("cases", [])):
            return {"scorecard_version": "perseus-vault-memory-quality-scorecard/v2", "verdict": "blocked", "blocking": True, "reason": "incomplete_case_contract"}
    failed_categories = set()
    invalid_cases = []
    observed_passed = 0
    observed_total = 0
    for case in report.get("cases", []):
        checks = case.get("checks", {}) or {}
        if "passed" not in checks and "total" not in checks:
            case_total = len(checks)
            case_passed = sum(1 for value in checks.values() if value is True)
        else:
            case_passed = _strict_count(checks.get("passed"))
            case_total = _strict_count(checks.get("total"))
        case_id = case.get("id") or case.get("category") or "<unknown>"
        status = case.get("status", "passed")
        if status == "unavailable" and case_passed == 0 and case_total == 0:
            continue
        if (
            case_passed is None
            or case_total is None
            or case_passed < 0
            or case_total <= 0
            or case_passed > case_total
        ):
            invalid_cases.append(case_id)
            if case.get("category"):
                failed_categories.add(case["category"])
            continue
        observed_passed += case_passed
        observed_total += case_total
        if status != "passed" or case_passed < case_total:
            if case.get("category"):
                failed_categories.add(case["category"])
    failed_categories = sorted(category for category in failed_categories if category)
    required_categories = _required_categories(report)
    actual_categories = {case.get("category") for case in report.get("cases", [])}
    missing = sorted(
        set(report.get("missing_categories", []))
        | (required_categories - actual_categories)
    )
    unavailable_categories, unavailable_cases = _unavailable_categories(report)
    unavailable_capabilities = _blocking_capabilities(report)
    metrics = report.get("metrics", {}) or {}
    metric_rates = report.get("metric_rates", {}) or {}
    unavailable_metrics = set(
        name
        for name, metric in metrics.items()
        if isinstance(metric, dict) and metric.get("status") in {"unavailable"}
    )
    failed_metrics = set(
        name
        for name, metric in metrics.items()
        if isinstance(metric, dict) and metric.get("status") == "failed"
    )
    partial_metrics = set(
        name
        for name, metric in metrics.items()
        if isinstance(metric, dict) and metric.get("status") in {"partial", "not_measured"}
    )
    invalid_metrics = set()
    required_metric_rates = REQUIRED_METRIC_RATES | (V1_REQUIRED_METRIC_RATES if _is_v1_report(report) else set())
    for name in required_metric_rates:
        metric = metric_rates.get(name)
        if not isinstance(metric, dict):
            unavailable_metrics.add(name)
            continue
        status = metric.get("status")
        rate = metric.get("rate")
        if status in {"unavailable", "partial", "not_measured"} or rate is None:
            unavailable_metrics.add(name)
        elif status == "failed":
            failed_metrics.add(name)
        elif status != "available":
            invalid_metrics.add(name)
        else:
            numeric_rate = _strict_number(rate)
            if numeric_rate is None or not 0.0 <= numeric_rate <= 1.0:
                invalid_metrics.add(name)
            elif name == "stale_recall_rate" and numeric_rate != 0.0:
                invalid_metrics.add(name)
    numeric_accuracy = _strict_number(report.get("accuracy"))
    accuracy = numeric_accuracy
    checks_passed = _strict_count(report.get("checks_passed"))
    checks_total = _strict_count(report.get("checks_total"))
    counts_match_cases = (
        checks_passed is not None
        and checks_total is not None
        and checks_passed == observed_passed
        and checks_total == observed_total
    )
    counts_consistent = (
        counts_match_cases
        and checks_total is not None
        and checks_passed is not None
        and checks_total > 0
        and checks_passed == checks_total
    )
    exact_accuracy = accuracy is not None and math.isfinite(accuracy) and accuracy == MINIMUM_ACCURACY
    case_count = len(report.get("cases", []))
    case_count_valid = 20 <= case_count <= 40 if _is_v1_report(report) else (20 <= case_count <= 30 if _is_v0_report(report) else case_count == 4)
    release_ready = (
        report.get("passed") is True
        and exact_accuracy
        and counts_consistent
        and not failed_categories
        and not missing
        and not unavailable_categories
        and not unavailable_capabilities
        and not unavailable_metrics
        and not partial_metrics
        and not failed_metrics
        and not invalid_metrics
        and not invalid_cases
        and counts_match_cases
        and case_count_valid
    )
    return {
        "scorecard_version": "perseus-vault-memory-quality-scorecard/v2",
        "benchmark": report.get("benchmark"),
        "dataset": report.get("dataset"),
        "harness_version": report.get("harness_version"),
        "verdict": "release_ready" if release_ready else "blocked",
        "blocking": True,
        "checks_passed": checks_passed if checks_passed is not None else 0,
        "checks_total": checks_total if checks_total is not None else 0,
        "accuracy": accuracy,
        "failed_categories": failed_categories,
        "invalid_cases": sorted(invalid_cases),
        "missing_categories": missing,
        "unavailable_categories": unavailable_categories,
        "unavailable_cases": unavailable_cases,
        "unavailable_capabilities": unavailable_capabilities,
        "unavailable_metrics": sorted(unavailable_metrics),
        "partial_metrics": sorted(partial_metrics),
        "failed_metrics": sorted(failed_metrics),
        "invalid_metrics": sorted(invalid_metrics),
        "case_count": case_count,
        "case_count_valid": case_count_valid,
        "metrics": metrics,
        "metric_rates": metric_rates,
        "thresholds": {
            "minimum_accuracy": MINIMUM_ACCURACY,
            "all_required_categories_present": True,
            "all_category_checks_pass": True,
            "no_unavailable_required_cases": True,
            "no_unavailable_capabilities": True,
            "no_unavailable_metrics": True,
            "no_failed_metrics": True,
            "exact_accuracy": True,
            "consistent_check_counts": True,
            "counts_match_cases": True,
            "case_count_20_to_40_for_v1": True,
        },
        "override_policy": {
            "allowed": True,
            "required_approver": REQUIRED_APPROVER,
            "requirements": [
                "document the failing checks and user impact",
                "link a remediation issue",
                "record the override in release notes",
            ],
        },
        "source_report_signature": report.get("result_signature_sha256"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    scorecard = build_scorecard(report)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(scorecard, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(scorecard, indent=2, sort_keys=True, allow_nan=False))
    return 0 if scorecard["verdict"] == "release_ready" else 1


if __name__ == "__main__":
    sys.exit(main())
