from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from .evaluator import aggregate_metrics
from .protocol import (
    ACCEPTANCE_SCHEMA,
    canonical_json,
    content_signature,
    sanitize_public_projection,
    sha256_text,
    validate_case_bundle,
    validate_manifest,
)


def _same_metrics(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return set(left) == set(right) and all(float(left[key]) == float(right[key]) for key in left)


def accept_run(
    manifest: dict[str, Any],
    bundle: dict[str, Any],
    run_return: dict[str, Any],
    *,
    case_file_sha256: str,
) -> dict[str, Any]:
    """Independently validate a sanitized run return.

    A complete failed benchmark is accepted as evidence with ``release_ready``
    false. Malformed, blocked, or incomplete runs are rejected as evidence.
    """
    errors: list[str] = []
    checks: dict[str, bool] = {}
    try:
        validate_manifest(manifest)
        validate_case_bundle(bundle, max_cases=manifest["config"].get("max_cases", 30))
        checks["manifest_valid"] = True
    except Exception as exc:
        checks["manifest_valid"] = False
        errors.append(f"manifest:{type(exc).__name__}")

    expected_manifest = sha256_text(canonical_json(manifest))
    checks["manifest_binding"] = run_return.get("manifest_sha256") == expected_manifest
    checks["case_file_binding"] = run_return.get("case_file_sha256") == case_file_sha256
    case_results = run_return.get("case_results", [])
    actual_ids = [item.get("case_id") for item in case_results if isinstance(item, dict)]
    expected_ids = list(manifest.get("case_ids", []))
    checks["case_ids"] = actual_ids == expected_ids and len(actual_ids) == len(set(actual_ids))
    required_categories = set(manifest.get("required_categories", []))
    actual_categories = {item.get("category") for item in case_results if isinstance(item, dict)}
    checks["category_coverage"] = required_categories.issubset(actual_categories)
    checks["completion"] = run_return.get("status") == "complete"
    metadata = run_return.get("provider_metadata")
    metadata_valid = (
        isinstance(metadata, dict)
        and isinstance(metadata.get("real_producer"), bool)
        and isinstance(metadata.get("offline"), bool)
        and isinstance(metadata.get("network_calls"), int)
        and not isinstance(metadata.get("network_calls"), bool)
        and metadata.get("network_calls", -1) >= 0
    )
    if metadata_valid and metadata["real_producer"]:
        binary_hash = metadata.get("binary_sha256")
        metadata_valid = isinstance(binary_hash, str) and re.fullmatch(r"[0-9a-f]{64}", binary_hash) is not None
    checks["provider_metadata"] = metadata_valid
    checks["report_signature"] = (
        isinstance(run_return.get("signature_sha256"), str)
        and run_return.get("signature_sha256") == content_signature(run_return)
    )

    try:
        recomputed = aggregate_metrics(case_results)
        checks["metric_recompute"] = _same_metrics(recomputed, run_return.get("metrics", {}))
    except Exception as exc:
        recomputed = {}
        checks["metric_recompute"] = False
        errors.append(f"metrics:{type(exc).__name__}")

    try:
        sanitize_public_projection(run_return)
        checks["public_boundary"] = True
    except Exception as exc:
        checks["public_boundary"] = False
        errors.append(f"public_boundary:{type(exc).__name__}")

    structural = all(checks.values())
    release_ready = structural and run_return.get("verdict") == "passed" and all(
        isinstance(item, dict) and item.get("status") == "passed" for item in case_results
    )
    report: dict[str, Any] = {
        "schema": ACCEPTANCE_SCHEMA,
        "suite_id": manifest.get("suite_id", "unknown"),
        "run_id": run_return.get("run_id", "unknown"),
        "acceptance_status": "accepted" if structural else "rejected",
        "release_ready": release_ready,
        "source_report_signature": run_return.get("signature_sha256", ""),
        "manifest_sha256": expected_manifest,
        "case_file_sha256": case_file_sha256,
        "case_count": len(case_results) if isinstance(case_results, list) else 0,
        "checks": checks,
        "metrics": recomputed,
        "errors": errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    clean = sanitize_public_projection(report)
    clean["signature_sha256"] = content_signature(clean)
    return clean
