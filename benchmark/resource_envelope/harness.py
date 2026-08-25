#!/usr/bin/env python3
"""Offline, hash-bound edge resource-envelope benchmark (#1105)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "perseus-vault-resource-envelope-manifest/v1"
OBSERVATION_SCHEMA = "perseus-vault-resource-observation/v1"
REPORT_SCHEMA = "perseus-vault-resource-envelope-report/v1"
OUTCOME_STATUSES = {
    "available",
    "empty",
    "partial",
    "unavailable",
    "timeout",
    "degraded",
}
OBSERVATION_STATUSES = {"available", "partial", "unavailable"}
PHASES = {"cold", "warm"}
RESOURCE_FIELDS = {
    "wall_time_ns",
    "cpu_time_ns",
    "peak_rss_bytes",
    "read_bytes",
    "write_bytes",
    "power_watts",
    "energy_joules",
}
WORKLOAD_FIELDS = {
    "measurement_overhead_ns",
    "net_wall_time_ns",
    "recall_candidate_count",
    "selected_count",
    "traversal_depth",
    "context_assembly_ns",
    "output_bytes",
    "output_tokens",
}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")


class ContractError(ValueError):
    """Fail-closed benchmark contract violation."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(f"{label} fields must be exactly {sorted(expected)}")


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(f"{label} must be a bounded safe identifier")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{label} must be a non-negative integer")
    return value


def _unique_by_id(
    values: list[dict[str, Any]], label: str
) -> dict[str, dict[str, Any]]:
    if not isinstance(values, list) or not values:
        raise ContractError(f"{label} must be a non-empty array")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        item_id = _safe_id(
            value.get("id") if isinstance(value, dict) else None, f"{label}.id"
        )
        if item_id in result:
            raise ContractError(f"duplicate {label} id: {item_id}")
        result[item_id] = value
    return result


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize the complete benchmark plan."""

    expected = {
        "schema_version",
        "benchmark_id",
        "benchmark_version",
        "vault_revision",
        "seed",
        "offline",
        "profiles",
        "corpora",
        "budgets",
        "cases",
        "tolerances",
        "driver",
    }
    _exact_keys(manifest, expected, "manifest")
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        raise ContractError("unsupported manifest schema")
    _safe_id(manifest["benchmark_id"], "benchmark_id")
    _safe_id(manifest["benchmark_version"], "benchmark_version")
    if not isinstance(manifest["vault_revision"], str) or not SHA1.fullmatch(
        manifest["vault_revision"]
    ):
        raise ContractError("vault_revision must be a full Git SHA")
    _nonnegative_int(manifest["seed"], "seed")
    if manifest["offline"] is not True:
        raise ContractError("resource-envelope benchmark must be offline")

    profiles = _unique_by_id(manifest["profiles"], "profiles")
    for profile in profiles.values():
        _exact_keys(
            profile,
            {
                "id",
                "hardware_class",
                "deployment_profile",
                "backend_runtime_manifest",
                "constraints",
                "profile_digest",
            },
            "profile",
        )
        _safe_id(profile["hardware_class"], "hardware_class")
        if profile["deployment_profile"] not in {
            "offline",
            "local_only",
            "local_with_approved_network",
            "external_actions_enabled",
        }:
            raise ContractError("unsupported deployment profile")
        _exact_keys(
            profile["backend_runtime_manifest"],
            {"model_backend", "embedding_backend", "storage_backend", "network"},
            "backend_runtime_manifest",
        )
        if profile["backend_runtime_manifest"]["network"] != "disabled":
            raise ContractError("offline benchmark profile cannot enable network")
        _exact_keys(
            profile["constraints"], {"cpu_threads", "memory_limit_mb"}, "constraints"
        )
        if _nonnegative_int(profile["constraints"]["cpu_threads"], "cpu_threads") < 1:
            raise ContractError("cpu_threads must be positive")
        if (
            _nonnegative_int(
                profile["constraints"]["memory_limit_mb"], "memory_limit_mb"
            )
            < 1
        ):
            raise ContractError("memory_limit_mb must be positive")
        digest = profile["profile_digest"]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ContractError("profile_digest must be SHA-256")
        unsigned = {
            key: value for key, value in profile.items() if key != "profile_digest"
        }
        if digest != sha256_json(unsigned):
            raise ContractError("profile_digest mismatch")

    corpora = _unique_by_id(manifest["corpora"], "corpora")
    for corpus in corpora.values():
        _exact_keys(
            corpus, {"id", "entity_count", "query_count", "fixture_sha256"}, "corpus"
        )
        _nonnegative_int(corpus["entity_count"], "entity_count")
        if _nonnegative_int(corpus["query_count"], "query_count") < 1:
            raise ContractError("query_count must be positive")
        if not isinstance(corpus["fixture_sha256"], str) or not SHA256.fullmatch(
            corpus["fixture_sha256"]
        ):
            raise ContractError("fixture_sha256 must be SHA-256")

    budgets = _unique_by_id(manifest["budgets"], "budgets")
    for budget in budgets.values():
        _exact_keys(
            budget,
            {"id", "recall_limit", "context_char_budget", "traversal_depth"},
            "budget",
        )
        if _nonnegative_int(budget["recall_limit"], "recall_limit") < 1:
            raise ContractError("recall_limit must be positive")
        if _nonnegative_int(budget["context_char_budget"], "context_char_budget") < 1:
            raise ContractError("context_char_budget must be positive")
        if _nonnegative_int(budget["traversal_depth"], "traversal_depth") > 8:
            raise ContractError("traversal_depth exceeds bound")

    _exact_keys(
        manifest["tolerances"],
        {"relative_spread_max", "minimum_repetitions"},
        "tolerances",
    )
    tolerance = manifest["tolerances"]["relative_spread_max"]
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or tolerance < 0
        or tolerance > 2
    ):
        raise ContractError("relative_spread_max must be finite and bounded")
    minimum_repetitions = _nonnegative_int(
        manifest["tolerances"]["minimum_repetitions"], "minimum_repetitions"
    )
    if minimum_repetitions < 2:
        raise ContractError("minimum_repetitions must be at least two")
    _exact_keys(manifest["driver"], {"id", "sha256"}, "driver")
    _safe_id(manifest["driver"]["id"], "driver.id")
    if not isinstance(manifest["driver"]["sha256"], str) or not SHA256.fullmatch(
        manifest["driver"]["sha256"]
    ):
        raise ContractError("driver digest must be SHA-256")

    cases = _unique_by_id(manifest["cases"], "cases")
    for case in cases.values():
        _exact_keys(
            case,
            {
                "id",
                "profile_id",
                "corpus_id",
                "budget_id",
                "phase",
                "scenario",
                "expected_outcomes",
                "repetitions",
            },
            "case",
        )
        if case["profile_id"] not in profiles:
            raise ContractError("case references unknown profile")
        if case["corpus_id"] not in corpora:
            raise ContractError("case references unknown corpus")
        if case["budget_id"] not in budgets:
            raise ContractError("case references unknown budget")
        if case["phase"] not in PHASES:
            raise ContractError("case phase must be cold or warm")
        if case["scenario"] not in OUTCOME_STATUSES:
            raise ContractError("case scenario is unsupported")
        expected_outcomes = case["expected_outcomes"]
        if (
            not isinstance(expected_outcomes, list)
            or not expected_outcomes
            or len(expected_outcomes) != len(set(expected_outcomes))
            or any(status not in OUTCOME_STATUSES for status in expected_outcomes)
        ):
            raise ContractError(
                "case expected_outcomes must be unique supported statuses"
            )
        if _nonnegative_int(case["repetitions"], "repetitions") < minimum_repetitions:
            raise ContractError("case repetitions below tolerance minimum")

    normalized = json.loads(json.dumps(manifest))
    for key in ("profiles", "corpora", "budgets", "cases"):
        normalized[key] = sorted(normalized[key], key=lambda item: item["id"])
    for case in normalized["cases"]:
        case["expected_outcomes"] = sorted(case["expected_outcomes"])
    normalized["tolerances"]["relative_spread_max"] = float(tolerance)
    return normalized


def _validate_observation_value(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) not in (
        {"status", "value"},
        {"status", "reason"},
    ):
        raise ContractError(
            f"{label} must be available with value or unavailable with reason"
        )
    status = raw.get("status")
    if status not in OBSERVATION_STATUSES:
        raise ContractError(f"{label} has unsupported observation status")
    if status == "available":
        value = raw.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ContractError(f"{label} value must be finite and non-negative")
        if "reason" in raw:
            raise ContractError(f"{label} available value cannot have reason")
    else:
        if (
            "value" in raw
            or not isinstance(raw.get("reason"), str)
            or not raw["reason"]
        ):
            raise ContractError(f"{label} non-available value requires reason")
    return dict(raw)


def validate_observations(
    manifest: dict[str, Any], observations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cases = {case["id"]: case for case in manifest["cases"]}
    if not isinstance(observations, list) or not observations:
        raise ContractError("observations must be a non-empty array")
    expected = {
        (case_id, repetition)
        for case_id, case in cases.items()
        for repetition in range(case["repetitions"])
    }
    seen: set[tuple[str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for raw in observations:
        _exact_keys(
            raw,
            {
                "schema_version",
                "case_id",
                "repetition",
                "outcome_status",
                "network_calls",
                "resources",
                "workload",
            },
            "observation",
        )
        if raw["schema_version"] != OBSERVATION_SCHEMA:
            raise ContractError("unsupported observation schema")
        case_id = _safe_id(raw["case_id"], "case_id")
        repetition = _nonnegative_int(raw["repetition"], "repetition")
        identity = (case_id, repetition)
        if identity not in expected or identity in seen:
            raise ContractError(
                "observation identity is missing, duplicate, or out of range"
            )
        seen.add(identity)
        if raw["outcome_status"] not in cases[case_id]["expected_outcomes"]:
            raise ContractError(
                "observation outcome is outside the frozen expected set"
            )
        if _nonnegative_int(raw["network_calls"], "network_calls") != 0:
            raise ContractError("offline benchmark recorded a network call")
        if (
            not isinstance(raw["resources"], dict)
            or set(raw["resources"]) != RESOURCE_FIELDS
        ):
            raise ContractError("resource observation field set mismatch")
        if (
            not isinstance(raw["workload"], dict)
            or set(raw["workload"]) != WORKLOAD_FIELDS
        ):
            raise ContractError("workload observation field set mismatch")
        clean = json.loads(json.dumps(raw))
        for name in RESOURCE_FIELDS:
            clean["resources"][name] = _validate_observation_value(
                clean["resources"][name], f"resources.{name}"
            )
        for name in WORKLOAD_FIELDS:
            clean["workload"][name] = _validate_observation_value(
                clean["workload"][name], f"workload.{name}"
            )
        wall = clean["resources"]["wall_time_ns"]
        overhead = clean["workload"]["measurement_overhead_ns"]
        net = clean["workload"]["net_wall_time_ns"]
        if all(item["status"] == "available" for item in (wall, overhead, net)) and net[
            "value"
        ] != max(0, wall["value"] - overhead["value"]):
            raise ContractError(
                "net wall time is not wall time minus measurement overhead"
            )
        normalized.append(clean)
    if seen != expected:
        raise ContractError("observation matrix is incomplete")
    return sorted(normalized, key=lambda item: (item["case_id"], item["repetition"]))


def _sample_rows(
    manifest: dict[str, Any], observations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cases = {case["id"]: case for case in manifest["cases"]}
    rows = []
    for observation in observations:
        case = cases[observation["case_id"]]
        rows.append(
            {
                **observation,
                "profile_id": case["profile_id"],
                "corpus_id": case["corpus_id"],
                "budget_id": case["budget_id"],
                "phase": case["phase"],
            }
        )
    return rows


def _median(values: list[float]) -> float | int | None:
    if not values:
        return None
    value = statistics.median(values)
    return int(value) if float(value).is_integer() else round(float(value), 6)


def _aggregate_dimensions(samples: list[dict[str, Any]]) -> dict[str, Any]:
    dimensions = {
        "profile": "profile_id",
        "corpus": "corpus_id",
        "budget": "budget_id",
        "phase": "phase",
        "outcome_status": "outcome_status",
    }
    output: dict[str, list[dict[str, Any]]] = {}
    for label, field in dimensions.items():
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sample in samples:
            groups[str(sample[field])].append(sample)
        rows = []
        for value, members in sorted(groups.items()):
            net_values = [
                item["workload"]["net_wall_time_ns"]["value"]
                for item in members
                if item["workload"]["net_wall_time_ns"]["status"] == "available"
            ]
            rss_values = [
                item["resources"]["peak_rss_bytes"]["value"]
                for item in members
                if item["resources"]["peak_rss_bytes"]["status"] == "available"
            ]
            rows.append(
                {
                    "value": value,
                    "sample_count": len(members),
                    "outcomes": dict(
                        sorted(
                            Counter(item["outcome_status"] for item in members).items()
                        )
                    ),
                    "median_net_wall_time_ns": _median(net_values),
                    "median_peak_rss_bytes": _median(rss_values),
                }
            )
        output[label] = rows
    return {"dimensions": output}


def _reproducibility(
    manifest: dict[str, Any], samples: list[dict[str, Any]]
) -> dict[str, Any]:
    tolerance = manifest["tolerances"]["relative_spread_max"]
    minimum = manifest["tolerances"]["minimum_repetitions"]
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        if sample["workload"]["net_wall_time_ns"]["status"] == "available":
            by_case[sample["case_id"]].append(sample)
    groups = []
    for case_id, members in sorted(by_case.items()):
        if len(members) < minimum:
            continue
        values = [item["workload"]["net_wall_time_ns"]["value"] for item in members]
        median = float(statistics.median(values))
        spread = (
            0.0
            if median == 0 and max(values) == min(values)
            else (float("inf") if median == 0 else (max(values) - min(values)) / median)
        )
        status = "within_tolerance" if spread <= tolerance else "outside_tolerance"
        groups.append(
            {
                "case_id": case_id,
                "repetitions": len(values),
                "median_net_wall_time_ns": int(median)
                if median.is_integer()
                else round(median, 6),
                "relative_spread": round(spread, 6),
                "tolerance": tolerance,
                "status": status,
            }
        )
    overall = (
        "within_tolerance"
        if groups and all(group["status"] == "within_tolerance" for group in groups)
        else "outside_tolerance"
    )
    return {"status": overall, "evaluated_groups": len(groups), "groups": groups}


def _raw_observations(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = {
        "schema_version",
        "case_id",
        "repetition",
        "outcome_status",
        "network_calls",
        "resources",
        "workload",
    }
    return [{key: sample[key] for key in keys} for sample in samples]


def build_report(
    manifest: dict[str, Any], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    manifest = validate_manifest(manifest)
    observations = validate_observations(manifest, observations)
    samples = _sample_rows(manifest, observations)
    report = {
        "schema_version": REPORT_SCHEMA,
        "status": "complete",
        "benchmark": "edge-resource-envelope",
        "manifest": manifest,
        "execution": {
            "offline": True,
            "network_calls": sum(item["network_calls"] for item in observations),
            "provider_calls": 0,
            "answerer_calls": 0,
            "judge_calls": 0,
        },
        "samples": samples,
        "aggregates": _aggregate_dimensions(samples),
        "reproducibility": _reproducibility(manifest, samples),
        "claims": {
            "label": "resource-envelope-observation",
            "low_swap_established": False,
            "partner_hardware_benchmarked": False,
            "product_efficacy": False,
            "power_inferred_from_cpu_or_memory": False,
        },
        "commitments": {
            "manifest_sha256": sha256_json(manifest),
            "observations_sha256": sha256_json(observations),
            "samples_sha256": sha256_json(samples),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "driver_sha256": manifest["driver"]["sha256"],
        },
    }
    report["report_sha256"] = sha256_json(report)
    validate_report(report)
    return report


def validate_report(report: dict[str, Any]) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_SCHEMA:
        raise ContractError("invalid report schema")
    digest = report.get("report_sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise ContractError("report digest is malformed")
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    if digest != sha256_json(unsigned):
        raise ContractError("report digest mismatch")
    manifest = validate_manifest(json.loads(json.dumps(report.get("manifest"))))
    samples = report.get("samples")
    if not isinstance(samples, list):
        raise ContractError("report samples are malformed")
    observations = _raw_observations(samples)
    normalized = validate_observations(manifest, observations)
    expected_samples = _sample_rows(manifest, normalized)
    if samples != expected_samples:
        raise ContractError("report sample dimensions do not match the manifest")
    commitments = report.get("commitments")
    required = {
        "manifest_sha256",
        "observations_sha256",
        "samples_sha256",
        "harness_sha256",
        "driver_sha256",
    }
    if not isinstance(commitments, dict) or set(commitments) != required:
        raise ContractError("report commitment fields are incomplete")
    if any(
        not isinstance(value, str) or not SHA256.fullmatch(value)
        for value in commitments.values()
    ):
        raise ContractError("report commitment is malformed")
    if commitments["manifest_sha256"] != sha256_json(manifest):
        raise ContractError("manifest commitment mismatch")
    if commitments["observations_sha256"] != sha256_json(normalized):
        raise ContractError("observations commitment mismatch")
    if commitments["samples_sha256"] != sha256_json(samples):
        raise ContractError("samples commitment mismatch")
    if commitments["driver_sha256"] != manifest["driver"]["sha256"]:
        raise ContractError("driver commitment mismatch")
    if (
        commitments["harness_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise ContractError("harness commitment mismatch")
    if report.get("aggregates") != _aggregate_dimensions(samples):
        raise ContractError("aggregate projection mismatch")
    if report.get("reproducibility") != _reproducibility(manifest, samples):
        raise ContractError("reproducibility projection mismatch")
    execution = report.get("execution")
    if execution != {
        "offline": True,
        "network_calls": 0,
        "provider_calls": 0,
        "answerer_calls": 0,
        "judge_calls": 0,
    }:
        raise ContractError("execution claim boundary mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default=str(Path(__file__).with_name("manifest.json"))
    )
    parser.add_argument(
        "--observations",
        default=str(Path(__file__).with_name("fixture_observations.json")),
    )
    parser.add_argument("--out", default=str(Path(__file__).with_name("report.json")))
    args = parser.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    observations = json.loads(Path(args.observations).read_text(encoding="utf-8"))
    report = build_report(manifest, observations)
    Path(args.out).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": report["status"], "report_sha256": report["report_sha256"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
