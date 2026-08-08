"""Build and validate the canonical public report envelope for suite runners."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from .artifacts import finalize_report, sha256_file, sha256_text, stable_json, write_report


def git_commit(root: str | Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def digest_manifest(manifest: Any) -> str:
    return sha256_text(stable_json(manifest))


def public_label(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._:/-]+", "_", str(value))
    forbidden = ("private", "query", "token", "credential", "password", "secret", "prompt", "body")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", text) or any(item in text.lower() for item in forbidden):
        return fallback
    return text


def safe_numeric(value: Any, *, integer: bool = False) -> Any:
    if isinstance(value, bool):
        return None
    if integer:
        return value if isinstance(value, int) and value >= 0 else None
    return value if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _safe_evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    allowed = {
        "available", "complete", "found", "matched", "numerator", "denominator",
        "count", "total", "rate", "status", "reason", "category", "capability",
        "mode", "scope", "failure_class", "raw_inputs_captured", "stage_trace_checked",
        "target_key_present", "truth_key_present", "contamination_key_present",
        "other_workspace_key_present", "receipt_present", "lease_released",
        "entities_archived", "entities_examined", "budget_chars", "injected_chars",
        "total_chars", "frozen_key_count", "on_demand_entities", "always_inject_entities",
    }
    result: dict[str, Any] = {}
    forbidden = ("private", "query", "token", "credential", "password", "secret", "prompt", "body")
    for key, value in raw.items():
        lowered = str(key).lower()
        if lowered not in allowed or lowered in {"query", "token", "credential", "private_key"}:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if isinstance(value, bool) or value is None:
            result[str(key)] = value
        elif isinstance(value, int):
            numeric = safe_numeric(value, integer=True)
            if numeric is not None:
                result[str(key)] = numeric
        elif isinstance(value, float):
            numeric = safe_numeric(value)
            if numeric is not None:
                result[str(key)] = numeric
        elif isinstance(value, str) and lowered in {"status", "reason", "category", "capability", "mode", "scope", "failure_class"}:
            label = public_label(value, "redacted_label")
            if label != "redacted_label":
                result[str(key)] = label
    return result


def _fallback_evidence(raw: Any) -> dict[str, Any]:
    """Provide a bounded execution marker when a suite has no public fields."""
    if not isinstance(raw, dict):
        return {"complete": True}
    safe = _safe_evidence(raw)
    return safe or {"complete": True}


def normalize_cases(raw_cases: list[dict[str, Any]], suite_id: str) -> list[dict[str, Any]]:
    """Convert a suite's internal rows into the strict common case contract."""
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            continue
        raw_id = raw.get("id") or raw.get("case") or f"case-{index + 1}"
        axis = raw.get("axis")
        case_id = f"{raw_id}-{axis}" if axis and not raw.get("id") else str(raw_id)
        assertions = raw.get("assertions")
        if isinstance(assertions, dict) and assertions:
            checks = {str(name): bool(value) for name, value in assertions.items()}
        elif "ok" in raw:
            checks = {"ok": bool(raw.get("ok"))}
        else:
            checks = {"executed": True}
        status = "passed" if all(checks.values()) else "failed"
        evidence = _fallback_evidence(raw.get("evidence", {}))
        if raw.get("failure_class"):
            failure_class = public_label(raw["failure_class"], "failure_class_redacted")
            if failure_class != "failure_class_redacted":
                evidence["failure_class"] = failure_class
        raw_status = raw.get("status")
        if raw_status in {"passed", "failed", "blocked", "unavailable", "not_measured"}:
            status = raw_status
        elif not checks:
            status = "failed"
        normalized.append({
            "id": public_label(case_id, f"case-{index + 1}"),
            "category": public_label(raw.get("category") or suite_id, suite_id),
            "status": status,
            "checks": checks,
            "evidence": evidence,
        })
    return normalized


def normalize_capabilities(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        return {"runner": {"status": "available"}}
    result: dict[str, dict[str, Any]] = {}
    for name, state in raw.items():
        key = str(name)
        key = public_label(key, "capability_unknown")
        if not isinstance(state, dict):
            result[key] = {"status": "unavailable", "reason": "invalid_capability_state"}
            continue
        status = state.get("status", "available")
        if status == "unknown":
            status = "unavailable"
        allowed = {"status": status}
        for field in ("reason", "required_tools", "missing_tools"):
            if field in state:
                allowed[field] = state[field]
        if status != "available":
            allowed["reason"] = public_label(allowed.get("reason"), "capability_unavailable")
        if status == "available":
            allowed.pop("missing_tools", None)
        if status not in {"available", "partial", "unavailable", "not_measured", "failed"}:
            status = "unavailable"
            allowed = {"status": status, "reason": "invalid_capability_status"}
        result[key] = allowed
    return result


def normalize_metrics(raw_metrics: Any, cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(raw_metrics, dict):
        for name, raw in raw_metrics.items():
            if not isinstance(raw, dict):
                continue
            status = raw.get("status", "available")
            metric: dict[str, Any] = {"status": status}
            if all(key in raw for key in ("numerator", "denominator")):
                numerator = safe_numeric(raw["numerator"], integer=True)
                denominator = safe_numeric(raw["denominator"], integer=True)
                if numerator is None or denominator is None or denominator == 0:
                    metric = {"status": "failed", "reason": "invalid_metric_numbers"}
                    status = "failed"
                else:
                    metric.update({"numerator": numerator, "denominator": denominator, "rate": numerator / denominator})
            elif all(key in raw for key in ("p50_ms", "p95_ms", "p99_ms")):
                metric.update({key: raw[key] for key in ("p50_ms", "p95_ms", "p99_ms")})
            elif "value" in raw:
                value = safe_numeric(raw["value"])
                if value is not None:
                    metric["value"] = value
            if status != "available":
                metric["reason"] = public_label(raw.get("reason") or "metric_not_available", "metric_not_available")
            if "numerator" not in metric and "p50_ms" not in metric and "value" not in metric and status == "available":
                continue
            metric_name = public_label(name, "metric_unknown")
            result[metric_name] = metric
    if not result:
        total = len(cases)
        passed = sum(case["status"] == "passed" for case in cases)
        result["suite_checks"] = {
            "status": "available" if total and passed == total else "failed",
            "numerator": passed,
            "denominator": max(1, total),
            "rate": passed / max(1, total),
        }
    return result


def build_common_report(
    *,
    suite_id: str,
    suite_version: str,
    raw_report: dict[str, Any],
    binary: str | Path,
    manifest: Any,
    profile: dict[str, Any],
    repo_root: str | Path,
    not_measured: list[str] | None = None,
    excluded: list[str] | None = None,
    claim_ids: list[str] | None = None,
    negative_claim_ids: list[str] | None = None,
) -> dict[str, Any]:
    cases = normalize_cases(raw_report.get("cases", []), suite_id)
    metrics = normalize_metrics(raw_report.get("metrics"), cases)
    capabilities = normalize_capabilities(raw_report.get("capabilities"))
    passed = bool(raw_report.get("passed", False)) and all(case["status"] == "passed" for case in cases)
    status = "passed" if passed and cases else ("failed" if cases else "blocked")
    control_digest = digest_manifest(profile)
    dataset_digest = digest_manifest(manifest)
    binary_digest = sha256_file(binary)
    commit = git_commit(repo_root)
    fingerprint = sha256_text(stable_json({
        "binary_sha256": binary_digest,
        "control_profile_sha256": control_digest,
        "dataset_sha256": dataset_digest,
        "harness_commit": commit,
    }))
    candidate = {
        "schema_version": "perseus-vault-benchmark-report/v1",
        "benchmark_id": suite_id,
        "suite_version": suite_version,
        "control_profile_sha256": control_digest,
        "run_fingerprint_sha256": fingerprint,
        "status": status,
        "capabilities": capabilities,
        "cases": cases,
        "metrics": metrics,
        "excluded": sorted(excluded or []),
        "negative_claim_ids": sorted(negative_claim_ids or []),
        "not_measured": sorted(not_measured or []),
        "public_evidence": "hash-only",
        "raw_inputs_captured": False,
        "network_calls": int(raw_report.get("network_calls", 0)),
        "binary_sha256": binary_digest,
        "dataset_sha256": dataset_digest,
        "harness_commit": commit,
        "claim_ids": sorted(claim_ids or []),
    }
    for key in (
        "benchmark", "dataset", "harness_version", "offline", "binary", "passed",
        "checks_passed", "checks_total", "accuracy", "signature_sha256", "required_categories", "metric_rates",
    ):
        if key in raw_report:
            candidate[key] = raw_report[key]
    # A suite's explicit negative-claim register is the source of truth. Keep
    # the common report publishable while binding the claim IDs that explain
    # what was and was not measured.
    return finalize_report(candidate)


def write_common_report(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    report = build_common_report(**kwargs)
    write_report(path, report)
    return report


__all__ = ["build_common_report", "digest_manifest", "git_commit", "normalize_cases", "write_common_report"]
