"""Offline all-required-evidence sufficiency curves for LongMemEval.

The evaluator runs only after a retrieval output has been sealed/replayed. Gold
sets are evaluator input, never production retrieval input. Public report rows
contain counts, ranks, and set commitments—not evidence IDs, prompts, or memory
bodies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

SUFFICIENCY_SCHEMA_VERSION = "perseus-vault-longmemeval-sufficiency/v1"
_DEFAULT_KS = (1, 3, 5, 10, 20, 50)
_RECORD_FIELDS = frozenset(
    {
        "question_id", "question_type", "required_evidence", "latest_evidence", "temporal_anchors",
        "ranked_ids", "status", "stale_evidence",
    }
)
_REQUIRED_RECORD_FIELDS = _RECORD_FIELDS - {"stale_evidence"}
_STATUSES = frozenset({"available", "partial", "unavailable", "truncated", "duplicate"})
_REPORT_FIELDS = frozenset(
    {
        "schema_version", "benchmark", "metric", "ks", "dataset_sha256", "fixture_sha256",
        "retrieval_config_sha256", "code_sha256", "question_count", "strata", "cases", "status_counts",
        "stale_evidence_cases", "offline", "provider_calls", "answerer_calls", "judge_calls",
        "raw_inputs_captured", "projection_sha256", "signature_sha256",
    }
)
_CASE_FIELDS = frozenset(
    {
        "question_id", "question_type", "status", "metric_status", "required_count", "ranked_count",
        "required_set_sha256", "ranked_set_sha256", "latest_set_sha256", "temporal_set_sha256",
        "single_hit_rank", "worst_required_rank", "missing_required_count", "latest_missing_count",
        "temporal_anchor_missing_count", "stale_exposure_count", "stale_required_missing_count",
    }
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class SufficiencyError(ValueError):
    """Raised when a sufficiency input or report is malformed or tampered."""


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SufficiencyError("value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise SufficiencyError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SufficiencyError(f"{name} must be a bounded identifier")
    return value


def _ids(value: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise SufficiencyError(f"{name} must be a list")
    result = [_id(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if len(result) != len(set(result)):
        raise SufficiencyError(f"{name} contains duplicate evidence IDs")
    return result


def _set_commitment(values: Iterable[str], *, ordered: bool) -> str | None:
    items = list(values)
    if not items:
        return None
    return sha256_json(items if ordered else sorted(items))


def _normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise SufficiencyError("each retrieval record must be an object")
    missing = _REQUIRED_RECORD_FIELDS - set(record)
    unknown = set(record) - _RECORD_FIELDS
    if missing:
        raise SufficiencyError(f"retrieval record missing field: {sorted(missing)[0]}")
    if unknown:
        raise SufficiencyError(f"retrieval record contains unknown field: {sorted(unknown)[0]}")
    qid = _id(record["question_id"], "question_id")
    qtype = _id(record["question_type"], "question_type")
    required = _ids(record["required_evidence"], "required_evidence", allow_empty=False)
    latest = _ids(record["latest_evidence"], "latest_evidence")
    temporal = _ids(record["temporal_anchors"], "temporal_anchors")
    stale = _ids(record.get("stale_evidence", []), "stale_evidence")
    if not set(latest) <= set(required):
        raise SufficiencyError("latest_evidence must be a subset of required_evidence")
    if not set(temporal) <= set(required):
        raise SufficiencyError("temporal_anchors must be a subset of required_evidence")
    if not set(stale) <= set(required):
        raise SufficiencyError("stale_evidence must be a subset of required_evidence")
    status = record["status"]
    if status not in _STATUSES - {"duplicate"}:
        raise SufficiencyError(f"unsupported retrieval status: {status}")
    ranked = record["ranked_ids"]
    if ranked is not None:
        if not isinstance(ranked, list):
            raise SufficiencyError("ranked_ids must be a list or null for unavailable retrieval")
        ranked = [_id(item, f"ranked_ids[{index}]") for index, item in enumerate(ranked)]
    if status == "available" and ranked is None:
        raise SufficiencyError("available retrieval requires ranked_ids")
    if status in {"partial", "truncated"} and ranked is None:
        raise SufficiencyError(f"{status} retrieval requires the observed ranked prefix")
    duplicate = ranked is not None and len(ranked) != len(set(ranked))
    if duplicate:
        status = "duplicate"
    return {
        "question_id": qid,
        "question_type": qtype,
        "required_evidence": required,
        "latest_evidence": latest,
        "temporal_anchors": temporal,
        "stale_evidence": stale,
        "ranked_ids": ranked,
        "status": status,
    }


def _rank_map(record: Mapping[str, Any]) -> dict[str, int] | None:
    if record["status"] in {"unavailable", "duplicate"} or record["ranked_ids"] is None:
        return None
    ranked = record["ranked_ids"]
    if len(ranked) != len(set(ranked)):
        return None
    return {item: index + 1 for index, item in enumerate(ranked)}


def _metric_eligible(record: Mapping[str, Any]) -> bool:
    return record["status"] == "available" and _rank_map(record) is not None


def _metric_row(records: list[Mapping[str, Any]], evidence_key: str, ks: tuple[int, ...]) -> dict[str, Any]:
    applicable = [record for record in records if record[evidence_key] and _metric_eligible(record)]
    statuses = {status: 0 for status in sorted(_STATUSES)}
    for record in records:
        if not _metric_eligible(record):
            statuses[record["status"]] += 1
    result: dict[str, Any] = {}
    for k in ks:
        numerator = 0
        for record in applicable:
            ranks = [_rank_map(record).get(item) for item in record[evidence_key]]
            if all(rank is not None and rank <= k for rank in ranks):
                numerator += 1
        denominator = len(applicable)
        result[f"@{k}"] = {
            "numerator": numerator,
            "denominator": denominator,
            "rate": round(numerator / denominator, 4) if denominator else None,
            "status_counts": statuses,
        }
    return result


def _single_hit_row(records: list[Mapping[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    applicable = [record for record in records if record["required_evidence"] and _metric_eligible(record)]
    statuses = {status: 0 for status in sorted(_STATUSES)}
    for record in records:
        if not _metric_eligible(record):
            statuses[record["status"]] += 1
    result: dict[str, Any] = {}
    for k in ks:
        numerator = 0
        for record in applicable:
            ranks = [_rank_map(record).get(item) for item in record["required_evidence"]]
            if any(rank is not None and rank <= k for rank in ranks):
                numerator += 1
        denominator = len(applicable)
        result[f"@{k}"] = {
            "numerator": numerator,
            "denominator": denominator,
            "rate": round(numerator / denominator, 4) if denominator else None,
            "status_counts": statuses,
        }
    return result


def _curves(records: list[Mapping[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    return {
        "single_hit_recall_at_k": _single_hit_row(records, ks),
        "all_required_coverage_at_k": _metric_row(records, "required_evidence", ks),
        "latest_version_coverage_at_k": _metric_row(records, "latest_evidence", ks),
        "temporal_anchor_coverage_at_k": _metric_row(records, "temporal_anchors", ks),
    }


def _stratum(records: list[Mapping[str, Any]], ks: tuple[int, ...]) -> dict[str, Any]:
    return {"question_count": len(records), "curves": _curves(records, ks)}


def _public_case(record: Mapping[str, Any]) -> dict[str, Any]:
    ranks = _rank_map(record)
    required = record["required_evidence"]
    latest = record["latest_evidence"]
    temporal = record["temporal_anchors"]
    stale = record["stale_evidence"]
    if ranks is None:
        return {
            "question_id": record["question_id"],
            "question_type": record["question_type"],
            "status": record["status"],
            "metric_status": "not-eligible",
            "required_count": len(required),
            "ranked_count": len(record["ranked_ids"] or []),
            "required_set_sha256": _set_commitment(required, ordered=False),
            "ranked_set_sha256": _set_commitment(record["ranked_ids"] or [], ordered=True),
            "latest_set_sha256": _set_commitment(latest, ordered=False),
            "temporal_set_sha256": _set_commitment(temporal, ordered=False),
            "single_hit_rank": None,
            "worst_required_rank": None,
            "missing_required_count": None,
            "latest_missing_count": None,
            "temporal_anchor_missing_count": None,
            "stale_exposure_count": None,
            "stale_required_missing_count": None,
        }
    required_ranks = [ranks.get(item) for item in required]
    latest_ranks = [ranks.get(item) for item in latest]
    temporal_ranks = [ranks.get(item) for item in temporal]
    stale_present = [item for item in stale if item in ranks]
    observed_required_ranks = [rank for rank in required_ranks if rank is not None]
    return {
        "question_id": record["question_id"],
        "question_type": record["question_type"],
        "status": record["status"],
        "metric_status": "eligible" if record["status"] == "available" else "not-eligible",
        "required_count": len(required),
        "ranked_count": len(record["ranked_ids"]),
        "required_set_sha256": _set_commitment(required, ordered=False),
        "ranked_set_sha256": _set_commitment(record["ranked_ids"], ordered=True),
        "latest_set_sha256": _set_commitment(latest, ordered=False),
        "temporal_set_sha256": _set_commitment(temporal, ordered=False),
        "single_hit_rank": min(observed_required_ranks) if observed_required_ranks else None,
        "worst_required_rank": max(required_ranks) if all(rank is not None for rank in required_ranks) else None,
        "missing_required_count": sum(rank is None for rank in required_ranks),
        "latest_missing_count": sum(rank is None for rank in latest_ranks) if latest else None,
        "temporal_anchor_missing_count": sum(rank is None for rank in temporal_ranks) if temporal else None,
        "stale_exposure_count": len(stale_present),
        "stale_required_missing_count": sum(item not in ranks for item in stale),
    }


def build_sufficiency_report(
    records: Iterable[Mapping[str, Any]],
    *,
    dataset_sha256: str,
    fixture_sha256: str,
    retrieval_config_sha256: str,
    code_sha256: str,
    ks: Iterable[int] = _DEFAULT_KS,
    focus_strata: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic judge/provider-free sufficiency report."""
    _sha(dataset_sha256, "dataset_sha256")
    _sha(fixture_sha256, "fixture_sha256")
    _sha(retrieval_config_sha256, "retrieval_config_sha256")
    _sha(code_sha256, "code_sha256")
    ks_tuple = tuple(ks)
    if not ks_tuple or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in ks_tuple):
        raise SufficiencyError("ks must contain positive integers")
    if len(set(ks_tuple)) != len(ks_tuple) or tuple(sorted(ks_tuple)) != ks_tuple:
        raise SufficiencyError("ks must be sorted and duplicate-free")
    normalized = [_normalize_record(record) for record in records]
    if not normalized:
        raise SufficiencyError("at least one retrieval record is required")
    qids = [record["question_id"] for record in normalized]
    if len(qids) != len(set(qids)):
        raise SufficiencyError("question IDs must be unique")
    normalized.sort(key=lambda record: record["question_id"])
    status_counts = {status: 0 for status in sorted(_STATUSES)}
    stale_cases = 0
    for record in normalized:
        status_counts[record["status"]] += 1
        if record["stale_evidence"]:
            stale_cases += 1
    strata: dict[str, Any] = {"overall": _stratum(normalized, ks_tuple), "by_question_type": {}, "focus": {}}
    question_types = sorted({record["question_type"] for record in normalized})
    for question_type in question_types:
        strata["by_question_type"][question_type] = _stratum(
            [record for record in normalized if record["question_type"] == question_type], ks_tuple
        )
    if focus_strata is not None:
        if not isinstance(focus_strata, Mapping):
            raise SufficiencyError("focus_strata must be an object")
        for name, types in sorted(focus_strata.items()):
            _id(name, "focus_strata name")
            if not isinstance(types, (list, tuple)) or not types:
                raise SufficiencyError("focus stratum must name at least one question type")
            type_list = [_id(item, f"focus_strata.{name}") for item in types]
            if len(type_list) != len(set(type_list)):
                raise SufficiencyError("focus stratum question types must be unique")
            strata["focus"][name] = _stratum(
                [record for record in normalized if record["question_type"] in set(type_list)], ks_tuple
            )
    cases = [_public_case(record) for record in normalized]
    base: dict[str, Any] = {
        "schema_version": SUFFICIENCY_SCHEMA_VERSION,
        "benchmark": "perseus-vault-longmemeval-retrieval-sufficiency",
        "metric": "single-hit, all-required, latest-version, and temporal-anchor coverage@k",
        "ks": list(ks_tuple),
        "dataset_sha256": dataset_sha256,
        "fixture_sha256": fixture_sha256,
        "retrieval_config_sha256": retrieval_config_sha256,
        "code_sha256": code_sha256,
        "question_count": len(normalized),
        "strata": strata,
        "cases": cases,
        "status_counts": status_counts,
        "stale_evidence_cases": stale_cases,
        "offline": True,
        "provider_calls": 0,
        "answerer_calls": 0,
        "judge_calls": 0,
        "raw_inputs_captured": False,
    }
    projection_sha256 = sha256_json(base)
    with_projection = {**base, "projection_sha256": projection_sha256}
    report = {**with_projection, "signature_sha256": sha256_json(with_projection)}
    validate_sufficiency_report(report)
    return report


def validate_sufficiency_report(report: Any) -> None:
    if not isinstance(report, Mapping):
        raise SufficiencyError("sufficiency report must be an object")
    unknown = set(report) - _REPORT_FIELDS
    missing = _REPORT_FIELDS - set(report)
    if unknown:
        raise SufficiencyError(f"report contains unknown field: {sorted(unknown)[0]}")
    if missing:
        raise SufficiencyError(f"report missing field: {sorted(missing)[0]}")
    if report["schema_version"] != SUFFICIENCY_SCHEMA_VERSION:
        raise SufficiencyError("unsupported sufficiency schema")
    for field in ("dataset_sha256", "fixture_sha256", "retrieval_config_sha256", "code_sha256", "projection_sha256", "signature_sha256"):
        _sha(report[field], field)
    if report["offline"] is not True or report["raw_inputs_captured"] is not False:
        raise SufficiencyError("report must be offline and raw-input free")
    for field in ("provider_calls", "answerer_calls", "judge_calls"):
        value = report[field]
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            raise SufficiencyError(f"{field} must be exactly zero")
    ks = report["ks"]
    if not isinstance(ks, list) or not ks or any(isinstance(k, bool) or not isinstance(k, int) or k <= 0 for k in ks):
        raise SufficiencyError("report ks is malformed")
    if ks != sorted(set(ks)):
        raise SufficiencyError("report ks must be sorted and unique")
    if isinstance(report["question_count"], bool) or not isinstance(report["question_count"], int) or report["question_count"] <= 0:
        raise SufficiencyError("question_count must be positive")
    cases = report["cases"]
    if not isinstance(cases, list) or len(cases) != report["question_count"]:
        raise SufficiencyError("case count does not match question_count")
    qids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping) or set(case) != _CASE_FIELDS:
            raise SufficiencyError(f"case {index} has malformed public fields")
        _id(case["question_id"], f"case {index}.question_id")
        if case["question_id"] in qids:
            raise SufficiencyError("public case IDs must be unique")
        qids.add(case["question_id"])
        _id(case["question_type"], f"case {index}.question_type")
        if case["status"] not in _STATUSES or case["metric_status"] not in {"eligible", "not-eligible"}:
            raise SufficiencyError(f"case {index} status is invalid")
        for field in ("required_set_sha256", "ranked_set_sha256", "latest_set_sha256", "temporal_set_sha256"):
            if case[field] is not None:
                _sha(case[field], f"case {index}.{field}")
    status_counts = report["status_counts"]
    if not isinstance(status_counts, Mapping) or set(status_counts) != _STATUSES:
        raise SufficiencyError("status_counts is incomplete")
    if sum(status_counts.values()) != len(cases) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in status_counts.values()
    ):
        raise SufficiencyError("status_counts does not match cases")
    base = {key: report[key] for key in report if key not in {"projection_sha256", "signature_sha256"}}
    if sha256_json(base) != report["projection_sha256"]:
        raise SufficiencyError("projection digest mismatch")
    signed = {**base, "projection_sha256": report["projection_sha256"]}
    if sha256_json(signed) != report["signature_sha256"]:
        raise SufficiencyError("report signature mismatch")
