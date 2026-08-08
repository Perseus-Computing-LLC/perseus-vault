"""Deterministic benchmark artifact helpers.

Public benchmark evidence is deliberately based on stable control/profile and
verdict material. Runtime identifiers, timestamps, credentials, and raw input
payloads must not enter the public signature.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SAFE_EVIDENCE_KEYS = {
    "available",
    "capability",
    "category",
    "check",
    "complete",
    "count",
    "denominator",
    "digest",
    "evidence_hash",
    "failure_class",
    "found",
    "matched",
    "mode",
    "numerator",
    "present",
    "profiles_compared",
    "rank",
    "ranked_key_count",
    "rate",
    "reason",
    "scope",
    "status",
    "target_key_present",
    "total",
    "truth_key_present",
    "contamination_key_present",
    "other_workspace_key_present",
    "receipt_present",
    "lease_released",
    "raw_inputs_captured",
    "stage_trace_checked",
    "entities_archived",
    "entities_examined",
    "budget_chars",
    "injected_chars",
    "total_chars",
    "frozen_key_count",
    "on_demand_entities",
    "always_inject_entities",
}
_SAFE_BOOLEAN_EVIDENCE_KEYS = {
    "available",
    "complete",
    "found",
    "matched",
    "present",
    "target_key_present",
    "truth_key_present",
    "contamination_key_present",
    "other_workspace_key_present",
    "receipt_present",
    "lease_released",
    "raw_inputs_captured",
    "stage_trace_checked",
}
_SAFE_INTEGER_EVIDENCE_KEYS = {
    "count",
    "denominator",
    "numerator",
    "rank",
    "ranked_key_count",
    "total",
    "entities_archived",
    "entities_examined",
    "budget_chars",
    "injected_chars",
    "total_chars",
    "frozen_key_count",
    "on_demand_entities",
    "always_inject_entities",
}
_SAFE_DIGEST_EVIDENCE_KEYS = {"digest", "evidence_hash"}
_SAFE_IDENTIFIER_EVIDENCE_KEYS = {
    "capability",
    "category",
    "check",
    "failure_class",
    "mode",
    "reason",
    "scope",
    "status",
}
_REPORT_KEYS = {
    "schema_version", "benchmark_id", "suite_version", "control_profile_sha256",
    "run_fingerprint_sha256", "status", "capabilities", "cases", "metrics",
    "not_measured", "excluded", "negative_claim_ids", "public_evidence", "raw_inputs_captured",
    "network_calls", "result_signature_sha256", "claim_ids", "binary_sha256",
    "dataset_sha256", "harness_commit", "benchmark", "dataset", "harness_version",
    "required_categories", "metric_rates", "offline", "binary", "passed",
    "checks_passed", "checks_total", "accuracy", "signature_sha256",
}
_CASE_KEYS = {"id", "category", "status", "checks", "evidence", "failure_class"}
_METRIC_KEYS = {"status", "numerator", "denominator", "rate", "p50_ms", "p95_ms", "p99_ms", "value", "reason"}
_PUBLIC_LABEL_FORBIDDEN = ("private", "query", "token", "credential", "password", "secret", "body")


def _is_public_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID_RE.fullmatch(value)) and not any(
        token in value.lower() for token in _PUBLIC_LABEL_FORBIDDEN
    )


def stable_json(value: Any) -> str:
    """Serialize canonical JSON and reject non-standard non-finite numbers."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _validate_finite_tree(value: Any, path: str = "value") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_finite_tree(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_finite_tree(child, f"{path}[{index}]")


def _validate_safe_evidence(evidence: Any, path: str = "evidence") -> None:
    if not isinstance(evidence, dict):
        raise ValueError(f"{path} must be an object")
    unknown = set(evidence) - _SAFE_EVIDENCE_KEYS
    if unknown:
        raise ValueError(f"{path} contains non-public fields: {sorted(unknown)}")
    for key, value in evidence.items():
        field_path = f"{path}.{key}"
        if key in _SAFE_BOOLEAN_EVIDENCE_KEYS:
            if not isinstance(value, bool):
                raise ValueError(f"{field_path} must be boolean")
            if key == "raw_inputs_captured" and value is not False:
                raise ValueError(f"{field_path} must be false")
        elif key in _SAFE_INTEGER_EVIDENCE_KEYS:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_path} must be a non-negative integer")
        elif key == "rate":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= value <= 1:
                raise ValueError(f"{field_path} must be a finite rate between 0 and 1")
        elif key in _SAFE_DIGEST_EVIDENCE_KEYS:
            if not _is_sha256(value):
                raise ValueError(f"{field_path} must be a lowercase SHA-256 digest")
        elif key == "profiles_compared":
            if not isinstance(value, list) or not value or any(not _is_public_identifier(item) for item in value):
                raise ValueError(f"{field_path} must contain bounded identifiers")
        elif key in _SAFE_IDENTIFIER_EVIDENCE_KEYS:
            if not _is_public_identifier(value):
                raise ValueError(f"{field_path} must be a bounded identifier")


def _validate_status_map(capabilities: Any) -> None:
    if not isinstance(capabilities, dict) or not capabilities:
        raise ValueError("capabilities must be a non-empty object")
    allowed = {"available", "partial", "unavailable", "not_measured", "failed"}
    allowed_state_keys = {"status", "reason", "required_tools", "missing_tools"}
    for name, state in capabilities.items():
        if not _is_public_identifier(name):
            raise ValueError("capability names must be bounded identifiers")
        if not isinstance(state, dict) or state.get("status") not in allowed:
            raise ValueError(f"capability {name} has an invalid status")
        if set(state) - allowed_state_keys:
            raise ValueError(f"capability {name} contains unknown fields")
        for key in ("required_tools", "missing_tools"):
            if key in state and (
                not isinstance(state[key], list)
                or any(not _is_public_identifier(tool) for tool in state[key])
            ):
                raise ValueError(f"capability {name}.{key} contains unsafe tool names")
        if state.get("status") in {"partial", "unavailable", "not_measured", "failed"} and not isinstance(state.get("reason"), str):
            raise ValueError(f"capability {name} requires a reason")
        if state.get("reason") is not None and not _is_public_identifier(state["reason"]):
            raise ValueError(f"capability {name} has an unsafe reason")
        missing_tools = set(state.get("missing_tools", []))
        required_tools = set(state.get("required_tools", []))
        if state.get("status") == "available" and missing_tools:
            raise ValueError(f"capability {name} is available with missing tools")
        if not missing_tools.issubset(required_tools):
            raise ValueError(f"capability {name} has missing tools outside required_tools")
        if state.get("status") in {"partial", "unavailable"} and required_tools and not missing_tools:
            raise ValueError(f"capability {name} is degraded without missing tools")


def validate_report(report: dict[str, Any]) -> None:
    """Fail-closed semantic validation for a publishable report.

    JSON Schema validates shape; this function validates cross-field invariants
    that JSON Schema alone cannot safely express, including evidence allowlists,
    complete denominators, and consistency between the declared status and all
    observed case/metric outcomes.
    """

    if not isinstance(report, dict):
        raise ValueError("report must be an object")
    _validate_finite_tree(report, "report")
    unknown_report = set(report) - _REPORT_KEYS
    if unknown_report:
        raise ValueError(f"report contains unknown fields: {sorted(unknown_report)}")
    required = {
        "schema_version",
        "benchmark_id",
        "suite_version",
        "control_profile_sha256",
        "run_fingerprint_sha256",
        "status",
        "capabilities",
        "cases",
        "metrics",
        "not_measured",
        "excluded",
        "public_evidence",
        "raw_inputs_captured",
        "result_signature_sha256",
        "binary_sha256",
        "dataset_sha256",
        "harness_commit",
    }
    missing = sorted(required - set(report))
    if missing:
        raise ValueError(f"report missing required fields: {', '.join(missing)}")
    if report["schema_version"] != "perseus-vault-benchmark-report/v1":
        raise ValueError("unsupported report schema_version")
    if not _is_public_identifier(report["benchmark_id"]):
        raise ValueError("benchmark_id must be a bounded identifier")
    if not _is_public_identifier(report["suite_version"]):
        raise ValueError("suite_version must be a bounded identifier")
    for key in ("control_profile_sha256", "run_fingerprint_sha256", "result_signature_sha256", "binary_sha256", "dataset_sha256"):
        if not _is_sha256(report[key]):
            raise ValueError(f"{key} must be a lowercase SHA-256 digest")
    if not isinstance(report["harness_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}|unknown", report["harness_commit"]):
        raise ValueError("harness_commit must be a bounded identifier")
    for optional_key in ("benchmark", "dataset", "harness_version", "binary"):
        if optional_key in report and not _is_public_identifier(report[optional_key]):
            raise ValueError(f"{optional_key} must be a public identifier")
    if "required_categories" in report and (
        not isinstance(report["required_categories"], list)
        or any(not _is_public_identifier(item) for item in report["required_categories"])
    ):
        raise ValueError("required_categories must contain public identifiers")
    if "metric_rates" in report and not isinstance(report["metric_rates"], dict):
        raise ValueError("metric_rates must be an object")
    if "offline" in report and not isinstance(report["offline"], bool):
        raise ValueError("offline must be boolean")
    if "passed" in report and not isinstance(report["passed"], bool):
        raise ValueError("passed must be boolean")
    for key in ("checks_passed", "checks_total"):
        if key in report and (not isinstance(report[key], int) or isinstance(report[key], bool) or report[key] < 0):
            raise ValueError(f"{key} must be a non-negative integer")
    if "checks_passed" in report and "checks_total" in report and report["checks_passed"] > report["checks_total"]:
        raise ValueError("checks_passed cannot exceed checks_total")
    if "accuracy" in report and (
        isinstance(report["accuracy"], bool)
        or not isinstance(report["accuracy"], (int, float))
        or not math.isfinite(float(report["accuracy"]))
        or not 0 <= report["accuracy"] <= 1
    ):
        raise ValueError("accuracy must be a finite rate")
    if "signature_sha256" in report and not _is_sha256(report["signature_sha256"]):
        raise ValueError("signature_sha256 must be a SHA-256 digest")
    if report["public_evidence"] != "hash-only" or report["raw_inputs_captured"] is not False:
        raise ValueError("publishable reports must be hash-only with raw_inputs_captured=false")
    if not isinstance(report.get("not_measured"), list) or not isinstance(report.get("excluded"), list):
        raise ValueError("not_measured and excluded must be arrays")
    if any(not _is_public_identifier(item) for item in report["not_measured"] + report["excluded"]):
        raise ValueError("not_measured and excluded entries must be bounded identifiers")
    if "negative_claim_ids" in report and (
        not isinstance(report["negative_claim_ids"], list)
        or any(not _is_public_identifier(item) for item in report["negative_claim_ids"])
    ):
        raise ValueError("negative_claim_ids must contain bounded identifiers")
    _validate_status_map(report["capabilities"])
    if "network_calls" in report and (not isinstance(report["network_calls"], int) or isinstance(report["network_calls"], bool) or report["network_calls"] < 0):
        raise ValueError("network_calls must be a non-negative integer")
    if "claim_ids" in report and (not isinstance(report["claim_ids"], list) or any(not _is_public_identifier(item) for item in report["claim_ids"])):
        raise ValueError("claim_ids must contain bounded identifiers")

    cases = report["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("report must contain at least one case")
    case_ids: set[str] = set()
    allowed_case_statuses = {"passed", "failed", "blocked", "unavailable", "not_measured"}
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("each case must be an object")
        unknown_case = set(case) - _CASE_KEYS
        if unknown_case:
            raise ValueError(f"case contains unknown fields: {sorted(unknown_case)}")
        for key in ("id", "category", "status", "checks", "evidence"):
            if key not in case:
                raise ValueError(f"case missing required field: {key}")
        case_id = case["id"]
        if not _is_public_identifier(case_id) or case_id in case_ids:
            raise ValueError("case IDs must be unique bounded identifiers")
        case_ids.add(case_id)
        if not _is_public_identifier(case["category"]):
            raise ValueError(f"case {case_id} has an unsafe category")
        if case["status"] not in allowed_case_statuses:
            raise ValueError(f"case {case_id} has an invalid status")
        checks = case["checks"]
        if not isinstance(checks, dict) or not checks:
            raise ValueError(f"case {case_id} must contain checks")
        if any(not _is_public_identifier(name) or not isinstance(value, bool) for name, value in checks.items()):
            raise ValueError(f"case {case_id} contains invalid checks")
        _validate_safe_evidence(case["evidence"], f"case {case_id}.evidence")
        if case["status"] == "passed" and not all(checks.values()):
            raise ValueError(f"case {case_id} is passed with a failed check")
        if case["status"] in {"failed", "blocked", "unavailable", "not_measured"} and all(checks.values()):
            raise ValueError(f"case {case_id} is non-passing with all checks true")
        if "failure_class" in case and not _is_public_identifier(case["failure_class"]):
            raise ValueError(f"case {case_id} has an unsafe failure_class")

    metrics = report["metrics"]
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError("report must contain at least one metric")
    allowed_metric_statuses = {"available", "partial", "unavailable", "not_measured", "failed"}
    allowed_metric_keys = _METRIC_KEYS
    for name, metric in metrics.items():
        if not _is_public_identifier(name) or not isinstance(metric, dict):
            raise ValueError("metrics must be keyed by bounded identifiers and contain objects")
        status = metric.get("status")
        if status not in allowed_metric_statuses:
            raise ValueError(f"metric {name} has an invalid status")
        if set(metric) - allowed_metric_keys:
            raise ValueError(f"metric {name} contains unknown fields")
        measured = any(key in metric for key in ("numerator", "denominator", "rate"))
        distribution = any(key in metric for key in ("p50_ms", "p95_ms", "p99_ms"))
        scalar = "value" in metric
        if status == "available" and not (measured or distribution or scalar):
            raise ValueError(f"available metric {name} lacks a measurement")
        if measured:
            numerator = metric.get("numerator")
            denominator = metric.get("denominator")
            rate = metric.get("rate")
            if not isinstance(numerator, int) or isinstance(numerator, bool) or numerator < 0:
                raise ValueError(f"metric {name}.numerator is invalid")
            if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator <= 0 or numerator > denominator:
                raise ValueError(f"metric {name}.denominator is invalid")
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(float(rate)) or not 0 <= rate <= 1:
                raise ValueError(f"metric {name}.rate is invalid")
            if abs(float(rate) - (numerator / denominator)) > 0.00005:
                raise ValueError(f"metric {name}.rate does not match numerator/denominator")
        if distribution:
            for key in ("p50_ms", "p95_ms", "p99_ms"):
                value = metric.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                    raise ValueError(f"metric {name}.{key} is invalid")
            if not metric["p50_ms"] <= metric["p95_ms"] <= metric["p99_ms"]:
                raise ValueError(f"metric {name} latency quantiles are not ordered")
        if scalar:
            value = metric["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"metric {name}.value is invalid")
        if "reason" in metric and not _is_public_identifier(metric["reason"]):
            raise ValueError(f"metric {name}.reason is not a bounded identifier")
        if status in {"unavailable", "not_measured", "failed", "partial"} and not isinstance(metric.get("reason"), str):
            raise ValueError(f"non-available metric {name} requires a reason")

    if report["status"] not in {"passed", "failed", "blocked", "partial"}:
        raise ValueError("report has an invalid status")
    if report["status"] == "partial":
        if not (
            report["not_measured"]
            or report["excluded"]
            or any(case["status"] != "passed" for case in cases)
            or any(metric.get("status") != "available" for metric in metrics.values())
            or any(state.get("status") != "available" for state in report["capabilities"].values())
        ):
            raise ValueError("partial report contains no partial, failed, unavailable, or explicitly unmeasured evidence")
    if report["status"] == "passed":
        if report["not_measured"] or report["excluded"]:
            raise ValueError("passed report cannot contain not_measured or excluded items")
        if any(case["status"] != "passed" for case in cases):
            raise ValueError("passed report contains a non-passing case")
        if any(metric.get("status") != "available" for metric in metrics.values()):
            raise ValueError("passed report contains a non-available metric")
        if any(state.get("status") != "available" for state in report["capabilities"].values()):
            raise ValueError("passed report contains a non-available capability")
    elif report["status"] == "failed":
        if (
            not any(case["status"] == "failed" for case in cases)
            and not any(metric.get("status") == "failed" for metric in metrics.values())
            and not any(state.get("status") == "failed" for state in report["capabilities"].values())
        ):
            raise ValueError("failed report contains no failed case, metric, or capability")
    elif report["status"] == "blocked":
        if (
            not report["not_measured"]
            and not report["excluded"]
            and not any(case["status"] in {"blocked", "unavailable", "not_measured"} for case in cases)
            and not any(metric.get("status") in {"partial", "unavailable", "not_measured"} for metric in metrics.values())
            and not any(state.get("status") in {"partial", "unavailable", "not_measured", "failed"} for state in report["capabilities"].values())
        ):
            raise ValueError("blocked report contains no blocking evidence")

    expected_signature = result_signature(report)
    if report["result_signature_sha256"] != expected_signature:
        raise ValueError("result_signature_sha256 does not match report verdicts")


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """Populate the semantic signature and validate the complete report."""

    candidate = dict(report)
    candidate.pop("result_signature_sha256", None)
    candidate["result_signature_sha256"] = result_signature(candidate)
    validate_report(candidate)
    return candidate


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    """Validate and write a publishable report with strict JSON serialization."""

    validate_report(report)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def control_profile_digest(profile: dict[str, Any]) -> str:
    """Return the digest of a canonical control profile."""

    return sha256_text(stable_json(profile))


def result_signature(report: dict[str, Any]) -> str:
    """Sign only deterministic case verdicts and metric outcomes.

    Evidence is intentionally excluded. This permits random runtime IDs and
    timestamps to be redacted without making equivalent verdicts incomparable.
    """

    cases = []
    for case in sorted(report.get("cases", []), key=lambda item: str(item.get("id", ""))):
        cases.append(
            {
                "id": case.get("id"),
                "category": case.get("category"),
                "status": case.get("status"),
                "checks": case.get("checks", {}),
                "failure_class": case.get("failure_class"),
            }
        )
    metrics = {}
    for name, metric in sorted((report.get("metrics") or {}).items()):
        if isinstance(metric, dict):
            metrics[name] = {
                key: metric[key]
                for key in ("status", "numerator", "denominator", "rate", "p50_ms", "p95_ms", "p99_ms", "value", "reason")
                if key in metric
            }
    payload = {
        "schema_version": report.get("schema_version"),
        "benchmark_id": report.get("benchmark_id"),
        "suite_version": report.get("suite_version"),
        "status": report.get("status"),
        "capabilities": report.get("capabilities", {}),
        "cases": cases,
        "metrics": metrics,
        "not_measured": sorted(report.get("not_measured", [])),
        "excluded": sorted(report.get("excluded", [])),
    }
    return sha256_text(stable_json(payload))


def run_fingerprint(*, binary_sha256: str, control_profile_sha256: str, dataset_sha256: str, harness_commit: str) -> str:
    return sha256_text(
        stable_json(
            {
                "binary_sha256": binary_sha256,
                "control_profile_sha256": control_profile_sha256,
                "dataset_sha256": dataset_sha256,
                "harness_commit": harness_commit,
            }
        )
    )


def write_json(path: str | Path, value: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


__all__ = [
    "control_profile_digest",
    "result_signature",
    "run_fingerprint",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "stable_json",
    "write_json",
]
