"""Build and validate the canonical public report envelope for suite runners."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any

from .artifacts import finalize_report, sha256_file, sha256_text, stable_json, validate_claim_arrays, write_report


def git_commit(root: str | Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(root), text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def git_commit_for_report(root: str | Path) -> str:
    commit = git_commit(root)
    if commit == "unknown" or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("harness commit unavailable")
    return commit


def digest_manifest(manifest: Any) -> str:
    return sha256_text(stable_json(manifest))


def _registered_claim_ids() -> set[str]:
    path = Path(__file__).resolve().parents[2] / "claim_register.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("claim register unavailable") from exc
    claims = data.get("claims")
    if not isinstance(claims, list):
        raise ValueError("claim register is malformed")
    registered = {
        item["id"]
        for item in claims
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if not registered:
        raise ValueError("claim register is empty")
    return registered


def digest_claims(claim_ids: list[str], negative_claim_ids: list[str]) -> str:
    validate_claim_arrays(claim_ids, negative_claim_ids)
    return sha256_text(stable_json({
        "claim_ids": sorted(claim_ids),
        "negative_claim_ids": sorted(negative_claim_ids),
    }))


_PUBLIC_ENUMS = {
    "status": {"available", "partial", "unavailable", "not_measured", "failed", "passed", "blocked"},
    "mode": {"fts5", "hybrid", "offline", "online", "logical_forget", "permanent_purge"},
}


def public_label(value: Any, fallback: str, *, field: str = "") -> str:
    text = str(value)
    if field in _PUBLIC_ENUMS:
        return text if text in _PUBLIC_ENUMS[field] else fallback
    if field in {"reason", "scope", "failure_class", "capability"}:
        if not isinstance(value, str) or not value:
            return fallback
        return sha256_text(value)
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", text):
        return fallback
    # Opaque caller-controlled labels are represented only by their digest.
    return text


def safe_numeric(value: Any, *, integer: bool = False) -> Any:
    if isinstance(value, bool):
        return None
    if integer:
        return value if isinstance(value, int) and value >= 0 and value.bit_length() <= 333 else None
    return value if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _safe_evidence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("evidence must be an object")
    allowed = {
        "available", "complete", "found", "matched", "numerator", "denominator",
        "count", "total", "rate", "status", "reason", "category", "capability",
        "mode", "scope", "failure_class", "raw_inputs_captured", "stage_trace_checked",
        "target_key_present", "truth_key_present", "contamination_key_present",
        "other_workspace_key_present", "receipt_present", "lease_released",
        "entities_archived", "entities_examined", "budget_chars", "injected_chars",
        "total_chars", "frozen_key_count", "on_demand_entities", "always_inject_entities",
        "always_inject_budget", "always_inject_injected_chars", "always_inject_total_chars",
        "on_demand_budget", "on_demand_injected_chars", "on_demand_total_chars",
        "history_total", "scoped_key_count", "ranked_key_count", "seed_count",
        "core_field_count", "core_field_total", "current_row_count", "authority_version",
        "external_ref_count", "provenance_field_count",
        "author_key_present", "other_key_present", "current_key_present",
        "superseded_evidence_present", "inside_found", "outside_found",
        "shared_profile_personal_key_present", "personal_profile_key_present",
        "live_key_present", "prior_version_content_present", "other_workspace_visible",
        "origin_present", "empty_abstained", "pending_health_present",
        "proposed_requires_review", "untrusted_authoritative", "hostile_marker_visible",
        "selected_a", "selected_b",
        "frozen_digest", "intent_hash", "outcome_hash", "temporal_digest",
        "empty_status", "pending_status", "proposed_outcome", "untrusted_outcome",
        "evidence_mode", "digest", "evidence_hash",
        "save_outcome_class", "drop_outcome_class", "block_outcome_class",
        "pending_outcome_class", "proposed_outcome_class",
        "save_serveable", "drop_no_raw_content", "block_no_raw_content",
        "pending_not_serveable", "proposed_not_serveable",
    }
    result: dict[str, Any] = {}
    forbidden = ("private", "query", "token", "credential", "password", "secret", "prompt", "body")
    for key, value in raw.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in forbidden):
            raise ValueError(f"evidence field {key} is forbidden")
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
        elif isinstance(value, str) and lowered in {"digest", "evidence_hash", "frozen_digest", "intent_hash", "outcome_hash", "temporal_digest"}:
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"evidence field {key} must be a lowercase SHA-256 digest")
            result[str(key)] = value
        elif isinstance(value, str) and lowered.endswith("_outcome_class"):
            if value not in {"save", "drop", "block", "pending_approval"}:
                raise ValueError(f"evidence field {key} has an invalid admission outcome class")
            result[str(key)] = value
        elif isinstance(value, str) and lowered in {"status", "reason", "category", "capability", "mode", "scope", "failure_class", "empty_status", "pending_status", "proposed_outcome", "untrusted_outcome", "evidence_mode"}:
            label = public_label(value, "redacted_label", field=lowered if lowered in {"status", "mode"} else "reason")
            if label != "redacted_label":
                result[str(key)] = label
    if not result:
        raise ValueError("evidence contains no public fields")
    return result


def _fallback_evidence(raw: Any) -> dict[str, Any]:
    """Provide a bounded execution marker when a suite has no public fields."""
    if not isinstance(raw, dict):
        raise ValueError("case evidence must be an object")
    return _safe_evidence(raw)


def normalize_cases(raw_cases: list[dict[str, Any]], suite_id: str) -> list[dict[str, Any]]:
    """Convert a suite's internal rows into the strict common case contract."""
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_cases):

        if not isinstance(raw, dict):
            raise ValueError(f"case {index} must be an object")
        raw_id = raw.get("id") or raw.get("case")
        if not isinstance(raw_id, str) or not raw_id:
            raise ValueError(f"case {index} is missing an id")
        axis = raw.get("axis")
        if axis and not raw.get("id"):
            case_id = f"{raw_id}-{axis}"
        else:
            case_id = str(raw_id)
        if not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", case_id):
            raise ValueError(f"case {raw_id} contains an unsafe id")
        assertions = raw.get("assertions")
        if isinstance(assertions, dict) and assertions:
            if any(not isinstance(name, str) or not isinstance(value, bool) for name, value in assertions.items()):
                raise ValueError(f"case {raw_id} contains invalid assertions")
            checks = dict(assertions)
        elif "ok" in raw:
            if not isinstance(raw["ok"], bool):
                raise ValueError(f"case {raw_id} contains non-boolean ok")
            checks = {"ok": raw["ok"]}
        elif isinstance(raw.get("checks"), dict) and raw["checks"]:
            if any(not isinstance(name, str) or not isinstance(value, bool) for name, value in raw["checks"].items()):
                raise ValueError(f"case {raw_id} contains invalid checks")
            checks = dict(raw["checks"])
        else:
            raise ValueError(f"case {raw_id} contains no assertions")
        status = "passed" if all(checks.values()) else "failed"
        evidence = _fallback_evidence(raw.get("evidence", {}))
        failure_class = raw.get("failure_class")
        if failure_class is not None:
            failure_class = public_label(failure_class, "failure_class_redacted", field="failure_class")
            if failure_class == "failure_class_redacted":
                raise ValueError(f"case {raw_id} contains an unsafe failure_class")
        raw_status = raw.get("status")
        if raw_status is not None and raw_status not in {"passed", "failed", "blocked", "unavailable", "not_measured"}:
            raise ValueError(f"case {raw_id} contains invalid status")
        if raw_status is not None:
            status = raw_status
        if status == "passed" and not all(checks.values()):
            raise ValueError(f"case {raw_id} is passed with a failed assertion")
        if status in {"failed", "blocked", "unavailable", "not_measured"} and all(checks.values()):
            raise ValueError(f"case {raw_id} is non-passing with all assertions true")
        if not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", case_id):
            raise ValueError(f"case {raw_id} contains an unsafe id")
        normalized_id = case_id
        existing_ids = {item["id"] for item in normalized}
        if normalized_id in existing_ids:
            raise ValueError(f"duplicate case id: {raw_id}")
        category_value = raw.get("category")
        if not isinstance(category_value, str) or not category_value:
            raise ValueError(f"case {raw_id} is missing a category")
        category = str(category_value)
        if not isinstance(category_value, str) or not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", category) or any(token in category.lower() for token in ("private", "query", "token", "credential", "password", "secret", "body")):
            raise ValueError(f"case {raw_id} contains an unsafe category")
        normalized.append({
            "id": normalized_id,
            "category": category,
            "status": status,
            "checks": checks,
            "evidence": evidence,
            **({"failure_class": failure_class} if failure_class is not None else {}),
        })
    return normalized


def normalize_capabilities(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("capabilities must be a non-empty object")
    result: dict[str, dict[str, Any]] = {}
    for name, state in raw.items():
        key = str(name)
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", key) or any(token in key.lower() for token in ("private", "query", "token", "credential", "password", "secret", "body")):
            raise ValueError("capability name is unsafe")
        if not isinstance(state, dict):
            raise ValueError(f"capability {key} must be an object")
        status = state.get("status")
        if status is None:
            raise ValueError(f"capability {key} is missing a status")
        if status not in {"available", "partial", "unavailable", "not_measured", "failed"}:
            raise ValueError(f"capability {key} has an invalid status")
        unknown = set(state) - {"status", "reason", "required_tools", "missing_tools", "details", "count"}
        if unknown:
            raise ValueError(f"capability {key} contains unknown fields")
        allowed = {"status": status}
        for field in ("reason", "required_tools", "missing_tools"):
            if field in state:
                allowed[field] = state[field]
        for field in ("required_tools", "missing_tools"):
            if field in allowed and (
                not isinstance(allowed[field], list)
                or any(not isinstance(tool, str) or not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", tool) for tool in allowed[field])
            ):
                raise ValueError(f"capability {key}.{field} contains unsafe tools")
        if "details" in state:
            if not isinstance(state["details"], dict):
                raise ValueError(f"capability {key}.details must be an object")
            if state["details"]:
                raise ValueError(f"capability {key}.details must be empty in public reports")
            allowed["details"] = {}
        if "count" in state:
            if not isinstance(state["count"], int) or isinstance(state["count"], bool) or state["count"] < 0:
                raise ValueError(f"capability {key}.count must be a non-negative integer")
            allowed["count"] = state["count"]
        if status != "available":
            reason = state.get("reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError(f"capability {key} requires a reason")
            allowed["reason"] = public_label(reason, "capability_unavailable", field="reason")
        if status == "available" and allowed.get("missing_tools"):
            raise ValueError(f"capability {key} is available with missing tools")
        if not set(allowed.get("missing_tools", [])).issubset(set(allowed.get("required_tools", []))):
            raise ValueError(f"capability {key} has missing tools outside required_tools")
        result[key] = allowed
    return result


def normalize_metrics(raw_metrics: Any, cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_metrics, dict):
        raise ValueError("metrics must be an object")
    if isinstance(raw_metrics, dict):
        for name, raw in raw_metrics.items():
            if not isinstance(raw, dict):
                raise ValueError(f"metric {name} must be an object")
            status = raw.get("status")
            if status is None:
                raise ValueError(f"metric {name} is missing a status")
            if status not in {"available", "partial", "unavailable", "not_measured", "failed"}:
                raise ValueError(f"metric {name} has an invalid status")
            if set(raw) - {"status", "numerator", "denominator", "rate", "p50_ms", "p95_ms", "p99_ms", "value", "reason"}:
                raise ValueError(f"metric {name} contains unknown fields")
            metric: dict[str, Any] = {"status": status}
            if all(key in raw for key in ("numerator", "denominator")):
                numerator = safe_numeric(raw["numerator"], integer=True)
                denominator = safe_numeric(raw["denominator"], integer=True)
                if numerator is None or denominator is None or denominator == 0 or numerator > denominator:
                    raise ValueError(f"metric {name} contains invalid numerator/denominator")
                computed_rate = numerator / denominator
                if "rate" in raw:
                    supplied_rate = safe_numeric(raw["rate"])
                    if supplied_rate is None or abs(float(supplied_rate) - computed_rate) > 0.00005:
                        raise ValueError(f"metric {name} rate does not match numerator/denominator")
                metric.update({"numerator": numerator, "denominator": denominator, "rate": computed_rate})
            elif all(key in raw for key in ("p50_ms", "p95_ms", "p99_ms")):
                values = {key: safe_numeric(raw[key]) for key in ("p50_ms", "p95_ms", "p99_ms")}
                if any(value is None or value < 0 for value in values.values()):
                    raise ValueError(f"metric {name} contains invalid latency quantiles")
                if not values["p50_ms"] <= values["p95_ms"] <= values["p99_ms"]:
                    raise ValueError(f"metric {name} latency quantiles are not ordered")
                metric.update(values)
            elif "value" in raw:
                value = safe_numeric(raw["value"])
                if value is None or value < 0:
                    raise ValueError(f"metric {name} contains an invalid scalar value")
                metric["value"] = value
            if status != "available":
                metric["reason"] = "metric_not_available"
            if "numerator" not in metric and "p50_ms" not in metric and "value" not in metric and status == "available":
                raise ValueError(f"metric {name} lacks a measured value")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", name):
                raise ValueError(f"metric {name} has an unsafe name")
            if name in result:
                raise ValueError(f"duplicate metric name: {name}")
            result[name] = metric
    if not result:
        raise ValueError("report must contain measured metrics")
    return result


def normalize_metric_rates(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("metric_rates must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    allowed_statuses = {"available", "partial", "unavailable", "not_measured", "failed"}
    for name, metric in raw.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9._:/-]{0,63}", name):
            raise ValueError("metric_rates contains an unsafe name")
        if not isinstance(metric, dict) or set(metric) - {"status", "rate"}:
            raise ValueError(f"metric_rates {name} is not a bounded object")
        status = metric.get("status")
        if status not in allowed_statuses:
            raise ValueError(f"metric_rates {name} has invalid status")
        if status == "available" and "rate" not in metric:
            raise ValueError(f"metric_rates {name} lacks a measured rate")
        rate = metric.get("rate")
        if "rate" in metric and rate is None:
            raise ValueError(f"metric_rates {name} has an explicit null rate")
        if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(float(rate)) or not 0 <= rate <= 1):
            raise ValueError(f"metric_rates {name} has invalid rate")
        if status in {"unavailable", "partial", "not_measured"} and rate is not None:
            raise ValueError(f"metric_rates {name} has a rate for non-measured status")
        # Failed metrics may retain their observed rate for diagnosis; the
        # scorecard still blocks publication based on the failed status.
        normalized[name] = {"status": status}
        if rate is not None:
            normalized[name]["rate"] = rate
    return normalized


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
    if "capabilities" not in raw_report:
        raise ValueError("raw report must declare capabilities")
    capabilities = normalize_capabilities(raw_report["capabilities"])
    passed = bool(raw_report.get("passed", False)) and all(case["status"] == "passed" for case in cases)
    has_degraded_capability = any(
        state.get("status") != "available" for state in capabilities.values() if isinstance(state, dict)
    )
    has_degraded_metric = any(
        metric.get("status") != "available" for metric in metrics.values() if isinstance(metric, dict)
    )
    status = (
        "passed"
        if passed and cases and not has_degraded_capability and not has_degraded_metric
        else "partial"
        if cases and (has_degraded_capability or has_degraded_metric or not passed)
        else "blocked"
    )
    control_digest = digest_manifest(profile)
    dataset_digest = digest_manifest(manifest)
    binary_digest = sha256_file(binary)
    commit = git_commit_for_report(repo_root)
    if not isinstance(claim_ids, list) or not isinstance(negative_claim_ids, list):
        raise ValueError("claim arrays must be explicitly supplied")
    claims_digest = digest_claims(claim_ids, negative_claim_ids)
    fingerprint = sha256_text(stable_json({
        "binary_sha256": binary_digest,
        "control_profile_sha256": control_digest,
        "dataset_sha256": dataset_digest,
        "harness_commit": commit,
        "claims_sha256": claims_digest,
    }))
    network_calls = raw_report.get("network_calls", 0)
    if not isinstance(network_calls, int) or isinstance(network_calls, bool) or network_calls < 0:
        raise ValueError("network_calls must be a non-negative integer")
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
        "network_calls": network_calls,
        "binary_sha256": binary_digest,
        "dataset_sha256": dataset_digest,
        "harness_commit": commit,
        "claim_ids": sorted(claim_ids),
        "claims_sha256": claims_digest,
    }
    for key in (
        "benchmark", "dataset", "harness_version", "offline", "binary", "passed",
        "checks_passed", "checks_total", "accuracy", "signature_sha256", "required_categories",
    ):
        if key in raw_report:
            if key == "metric_rates":
                continue
            candidate[key] = raw_report[key]
    if "metric_rates" in raw_report:
        candidate["metric_rates"] = normalize_metric_rates(raw_report["metric_rates"])
    # A suite's explicit negative-claim register is the source of truth. Keep
    # the common report publishable while binding the claim IDs that explain
    # what was and was not measured.
    return finalize_report(candidate)


def write_common_report(path: str | Path, **kwargs: Any) -> dict[str, Any]:
    report = build_common_report(**kwargs)
    write_report(path, report)
    return report


__all__ = ["build_common_report", "digest_claims", "digest_manifest", "git_commit", "git_commit_for_report", "normalize_cases", "normalize_metric_rates", "write_common_report"]
