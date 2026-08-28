from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import AdmissionReceipt, MemoryRecord, RetrievalResult
from .protocol import (
    RUN_SCHEMA,
    ProtocolError,
    canonical_json,
    content_signature,
    sanitize_public_projection,
    sha256_bytes,
    sha256_text,
    validate_case_bundle,
    validate_manifest,
)
from .provider import HostileMemoryProvider


@dataclass(frozen=True)
class ProbeObservation:
    probe_id: str
    passed: bool
    disposition: str
    expected_decision: str
    observed_decision: str
    reason_codes: tuple[str, ...]
    required_present: bool
    forbidden_present: bool
    scope_ok: bool
    provenance_ok: bool
    budget_ok: bool
    hit_count: int
    context_words: int

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "passed": self.passed,
            "disposition": self.disposition,
            "expected_decision": self.expected_decision,
            "observed_decision": self.observed_decision,
            "reason_codes": list(self.reason_codes),
            "required_present": self.required_present,
            "forbidden_present": self.forbidden_present,
            "scope_ok": self.scope_ok,
            "provenance_ok": self.provenance_ok,
            "budget_ok": self.budget_ok,
            "hit_count": self.hit_count,
            "context_words": self.context_words,
        }


@dataclass(frozen=True)
class AdmissionObservation:
    record_id: str
    expected_status: str | list[str] | None
    observed_status: str
    serveable: bool
    passed: bool
    reason_codes: tuple[str, ...]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "expected_status": self.expected_status,
            "admission_status": self.observed_status,
            "serveable": self.serveable,
            "passed": self.passed,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    status: str
    event_count: int
    probe_count: int
    passed_probes: int
    passed_checks: int
    admissions: tuple[AdmissionObservation, ...]
    observations: tuple[ProbeObservation, ...]
    case_commitment: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "status": self.status,
            "event_count": self.event_count,
            "probe_count": self.probe_count,
            "passed_probes": self.passed_probes,
            "passed_checks": self.passed_checks,
            "admissions": [item.to_public_dict() for item in self.admissions],
            "observations": [item.to_public_dict() for item in self.observations],
            "case_commitment": self.case_commitment,
        }


def _hit_id(hit: Any) -> str:
    if isinstance(hit, dict):
        return str(hit.get("record_id", ""))
    return str(getattr(hit, "record_id", ""))


def _hit_scope(hit: Any) -> str:
    if isinstance(hit, dict):
        return str(hit.get("scope", ""))
    return str(getattr(hit, "scope", ""))


def _hit_text(hit: Any) -> str:
    if isinstance(hit, dict):
        return str(hit.get("text", ""))
    return str(getattr(hit, "text", ""))


def _has_provenance(hit: Any) -> bool:
    if isinstance(hit, dict):
        values = [hit.get(field) for field in ("source_ref", "record_digest", "actor", "trust")]
    else:
        values = [getattr(hit, field, None) for field in ("source_ref", "record_digest", "actor", "trust")]
    return all(isinstance(value, str) and value.strip() for value in values)


def grade_probe(probe: dict[str, Any], result: RetrievalResult) -> ProbeObservation:
    expected = probe["expected"]
    expected_decision = expected["decision"]
    hits = tuple(result.hits)
    ids = {_hit_id(hit) for hit in hits}
    required = set(expected.get("required_ids", []))
    forbidden = set(expected.get("forbidden_ids", []))
    required_present = required.issubset(ids)
    forbidden_present = bool(ids & forbidden)
    required_scope = expected["required_scope"]
    scope_ok = all(_hit_scope(hit) == required_scope for hit in hits)
    provenance_ok = all(_has_provenance(hit) for hit in hits)
    context_words = sum(len(_hit_text(hit).split()) for hit in hits)
    budget_ok = len(hits) <= int(probe["limit"]) and context_words <= int(expected["max_context_words"])
    reasons: list[str] = list(result.reason_codes)

    stale_present = False
    for hit in hits:
        status = hit.get("status", "active") if isinstance(hit, dict) else getattr(hit, "status", "active")
        valid_to = hit.get("valid_to") if isinstance(hit, dict) else getattr(hit, "valid_to", None)
        if status in {"archived", "quarantined"}:
            stale_present = True
        elif status == "superseded" and valid_to is not None and probe["as_of"] >= valid_to:
            stale_present = True
    if stale_present or (forbidden_present and bool(forbidden)):
        reasons.append("stale_leak")
    if not scope_ok:
        reasons.append("scope_leak")
    if not provenance_ok and expected.get("require_provenance", True):
        reasons.append("provenance_missing")
    if not budget_ok:
        reasons.append("context_budget_exceeded")
    if forbidden_present:
        reasons.append("forbidden_evidence")

    if result.decision in {"blocked", "error"}:
        disposition = result.decision
        reasons.append("capability_unavailable" if result.decision == "blocked" else "provider_error")
        passed = expected_decision == result.decision
    elif expected_decision == "abstain":
        if result.decision == "abstain" and not hits:
            disposition = "abstain"
            passed = not reasons or all(reason in {"no_trustworthy_evidence", "no_memory_control"} for reason in reasons)
        else:
            disposition = "wrong"
            reasons.append("unsupported_evidence_returned")
            passed = False
    elif expected_decision == "answer":
        if result.decision == "answer" and required_present and not forbidden_present and scope_ok and budget_ok and (
            provenance_ok or not expected.get("require_provenance", True)
        ):
            disposition = "correct"
            passed = True
        elif forbidden_present or not scope_ok or stale_present:
            disposition = "wrong"
            passed = False
        else:
            disposition = "miss"
            passed = False
            if not required_present:
                reasons.append("required_evidence_missing")
            if result.decision != "answer":
                reasons.append("answer_not_returned")
    else:
        disposition = "blocked"
        reasons.append("unsupported_expected_decision")
        passed = False

    return ProbeObservation(
        probe_id=probe["probe_id"],
        passed=passed,
        disposition=disposition,
        expected_decision=expected_decision,
        observed_decision=result.decision,
        reason_codes=tuple(dict.fromkeys(reasons)),
        required_present=required_present,
        forbidden_present=forbidden_present,
        scope_ok=scope_ok,
        provenance_ok=provenance_ok,
        budget_ok=budget_ok,
        hit_count=len(hits),
        context_words=context_words,
    )


def _admission_observation(event: dict[str, Any], receipt: AdmissionReceipt) -> AdmissionObservation:
    expected = event.get("expected_status")
    accepted_statuses = expected if isinstance(expected, list) else [expected]
    passed = expected is None or receipt.status in accepted_statuses
    reasons = list(receipt.reason_codes)
    if not passed:
        reasons.append("admission_status_mismatch")
    return AdmissionObservation(
        record_id=receipt.record_id,
        expected_status=expected,
        observed_status=receipt.status,
        serveable=receipt.serveable,
        passed=passed,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def evaluate_case(provider: HostileMemoryProvider, case: dict[str, Any]) -> CaseResult:
    provider.reset()
    admissions: list[AdmissionObservation] = []
    try:
        for event in case["events"]:
            if event["type"] == "ingest":
                record = MemoryRecord.from_dict(event["record"])
                admissions.append(_admission_observation(event, provider.ingest(record)))
            elif event["type"] == "forget":
                receipt = provider.forget(event["scope"], event["record_id"])
                expected = event.get("expected_status", "archived")
                admissions.append(AdmissionObservation(
                    record_id=receipt.record_id,
                    expected_status=expected,
                    observed_status=receipt.status,
                    serveable=False,
                    passed=receipt.status == expected,
                    reason_codes=receipt.reason_codes,
                ))
    except Exception as exc:
        case_commitment = sha256_text(canonical_json({"case_id": case["case_id"], "error": type(exc).__name__}))
        return CaseResult(
            case_id=case["case_id"], category=case["category"], status="error",
            event_count=len(case["events"]), probe_count=len(case["probes"]), passed_probes=0,
            passed_checks=0, admissions=tuple(admissions), observations=(), case_commitment=case_commitment,
        )

    observations: list[ProbeObservation] = []
    for probe in case["probes"]:
        try:
            result = provider.retrieve(probe["query"], probe["scope"], probe["as_of"], probe["limit"])
            observations.append(grade_probe(probe, result))
        except Exception as exc:
            observations.append(ProbeObservation(
                probe_id=probe["probe_id"], passed=False, disposition="error",
                expected_decision=probe["expected"]["decision"], observed_decision="error",
                reason_codes=(f"provider_error:{type(exc).__name__}",), required_present=False,
                forbidden_present=False, scope_ok=False, provenance_ok=False, budget_ok=False,
                hit_count=0, context_words=0,
            ))
    checks = sum(1 for item in admissions if item.passed) + sum(
        sum([item.passed, item.required_present or item.expected_decision == "abstain",
             not item.forbidden_present, item.scope_ok, item.provenance_ok or item.expected_decision == "abstain",
             item.budget_ok]) for item in observations
    )
    passed_probes = sum(item.passed for item in observations)
    status = "passed" if all(item.passed for item in admissions) and all(item.passed for item in observations) else "failed"
    commitment = sha256_text(canonical_json({
        "case_id": case["case_id"],
        "admissions": [item.to_public_dict() for item in admissions],
        "observations": [item.to_public_dict() for item in observations],
    }))
    return CaseResult(
        case_id=case["case_id"], category=case["category"], status=status,
        event_count=len(case["events"]), probe_count=len(case["probes"]),
        passed_probes=passed_probes, passed_checks=checks,
        admissions=tuple(admissions), observations=tuple(observations), case_commitment=commitment,
    )


def _flatten(results: Iterable[CaseResult | dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in results:
        if isinstance(result, CaseResult):
            out.extend(item.to_public_dict() for item in result.observations)
        else:
            out.extend(result.get("observations", []))
    return out


def aggregate_metrics(results: Iterable[CaseResult | dict[str, Any]]) -> dict[str, float]:
    materialized = list(results)
    public = [result.to_public_dict() if isinstance(result, CaseResult) else result for result in materialized]
    observations = [item for result in public for item in result.get("observations", [])]
    admissions = [item for result in public for item in result.get("admissions", [])]
    duplicate_admissions = [
        item
        for result in public
        if result.get("category") in {"duplicate_flood", "near_duplicate_flood", "replay_idempotency"}
        for item in result.get("admissions", [])
        if item.get("expected_status") != "admitted"
    ]
    n_cases = len(public)
    n_probes = len(observations)
    answer_probes = [item for item in observations if item.get("expected_decision") == "answer"]
    abstain_probes = [item for item in observations if item.get("expected_decision") == "abstain"]
    observed_abstains = [item for item in observations if item.get("disposition") == "abstain"]
    def rate(num: int, den: int) -> float:
        return round(num / den, 6) if den else 0.0
    return {
        "case_pass_rate": rate(sum(result.get("status") == "passed" for result in public), n_cases),
        "probe_pass_rate": rate(sum(item.get("passed") is True for item in observations), n_probes),
        "correct_evidence_rate": rate(sum(item.get("disposition") == "correct" for item in answer_probes), len(answer_probes)),
        "safe_abstention_rate": rate(sum(item.get("disposition") == "abstain" for item in abstain_probes), len(abstain_probes)),
        "abstention_precision": rate(sum(item.get("disposition") == "abstain" and item.get("expected_decision") == "abstain" for item in observed_abstains), len(observed_abstains)),
        "wrong_evidence_rate": rate(sum(item.get("disposition") == "wrong" for item in observations), n_probes),
        "stale_leak_rate": rate(sum("stale_leak" in item.get("reason_codes", []) for item in observations), n_probes),
        "scope_leak_rate": rate(sum("scope_leak" in item.get("reason_codes", []) for item in observations), n_probes),
        "provenance_completeness_rate": rate(sum(
            item.get("required_present") is True and item.get("provenance_ok") is True
            for item in answer_probes
        ), len(answer_probes)),
        "admission_contract_rate": rate(sum(item.get("passed") is True for item in admissions), len(admissions)),
        "context_budget_violation_rate": rate(sum(item.get("budget_ok") is False for item in observations), n_probes),
        "duplicate_replay_materialization_rate": rate(
            sum(item.get("admission_status") == "admitted" for item in duplicate_admissions),
            len(duplicate_admissions),
        ),
    }


def run_suite(provider: HostileMemoryProvider, manifest: dict[str, Any], bundle: dict[str, Any],
              *, case_file_sha256: str, manifest_sha256: str, run_id: str = "local") -> dict[str, Any]:
    validate_manifest(manifest)
    validate_case_bundle(bundle, max_cases=manifest["config"].get("max_cases", 30))
    cases_by_id = {case["case_id"]: case for case in bundle["cases"]}
    if set(cases_by_id) != set(manifest["case_ids"]):
        raise ProtocolError("manifest and case bundle IDs differ")
    results = [evaluate_case(provider, cases_by_id[case_id]) for case_id in manifest["case_ids"]]
    public_results = [result.to_public_dict() for result in results]
    categories = sorted({result.category for result in results})
    required_categories = set(manifest["required_categories"])
    missing_categories = sorted(required_categories - set(categories))
    metrics = aggregate_metrics(results)
    all_passed = all(result.status == "passed" for result in results) and not missing_categories
    metadata = provider.public_metadata()
    if not isinstance(metadata, dict):
        raise ProtocolError("provider public_metadata must be an object")
    run_return: dict[str, Any] = {
        "schema": RUN_SCHEMA,
        "suite_id": manifest["suite_id"],
        "run_id": run_id,
        "provider": provider.name,
        "provider_contract": provider.contract,
        "provider_metadata": metadata,
        "status": "complete",
        "verdict": "passed" if all_passed else "failed",
        "manifest_sha256": manifest_sha256,
        "case_file_sha256": case_file_sha256,
        "case_count": len(results),
        "probe_count": sum(result.probe_count for result in results),
        "passed_cases": sum(result.status == "passed" for result in results),
        "passed_probes": sum(result.passed_probes for result in results),
        "required_categories": sorted(required_categories),
        "categories": categories,
        "missing_categories": missing_categories,
        "case_results": public_results,
        "metrics": metrics,
        "capabilities": {
            "ingest": {"available": True, "status": "available"},
            "retrieve": {"available": True, "status": "available"},
            "forget": {"available": True, "status": "available"},
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    clean = sanitize_public_projection(run_return)
    clean["signature_sha256"] = content_signature(clean)
    return clean


def write_json(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def file_sha256(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())
