from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any


CASE_SCHEMA = "perseus-hostile-memory-gauntlet/cases/v1"
MANIFEST_SCHEMA = "perseus-hostile-memory-gauntlet/manifest/v1"
RUN_SCHEMA = "perseus-hostile-memory-gauntlet/run-return/v1"
ACCEPTANCE_SCHEMA = "perseus-hostile-memory-gauntlet/acceptance-report/v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(ValueError):
    """A malformed or unsafe benchmark contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_hex(value: Any, field: str) -> None:
    _require(isinstance(value, str) and _HEX64.fullmatch(value) is not None,
             f"{field} must be a lowercase SHA-256 hex digest")


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    _require(isinstance(manifest, Mapping), "manifest must be an object")
    _require(manifest.get("schema") == MANIFEST_SCHEMA, "unsupported manifest schema")
    suite_id = manifest.get("suite_id")
    _require(isinstance(suite_id, str) and suite_id.strip(), "manifest suite_id is required")
    case_ids = manifest.get("case_ids")
    _require(isinstance(case_ids, list) and case_ids, "manifest case_ids must be non-empty")
    _require(all(isinstance(x, str) and x.strip() for x in case_ids), "case_ids must be non-empty strings")
    _require(len(case_ids) == len(set(case_ids)), "manifest case_ids must be unique")
    categories = manifest.get("required_categories")
    _require(isinstance(categories, list) and categories, "required_categories must be non-empty")
    _require(len(categories) == len(set(categories)), "required_categories must be unique")
    config = manifest.get("config")
    _require(isinstance(config, Mapping), "manifest config is required")
    max_cases = config.get("max_cases", 30)
    _require(_is_int(max_cases) and 1 <= max_cases <= 100, "config.max_cases must be between 1 and 100")
    _require(len(case_ids) <= max_cases, "manifest exceeds config.max_cases")
    if "case_file" in manifest:
        _require(isinstance(manifest["case_file"], str) and manifest["case_file"].strip(),
                 "case_file must be a non-empty path")
    if "case_file_sha256" in manifest:
        _check_hex(manifest["case_file_sha256"], "case_file_sha256")


def _validate_record(record: Mapping[str, Any]) -> None:
    required = (
        "record_id", "memory_key", "scope", "text", "source_ref", "record_digest",
        "actor", "trust", "valid_from", "recorded_at",
    )
    for field in required:
        _require(field in record, f"record missing {field}")
    for field in ("record_id", "memory_key", "scope", "text", "source_ref", "actor", "trust"):
        _require(isinstance(record[field], str) and record[field].strip(), f"record.{field} is required")
    _check_hex(record["record_digest"], "record.record_digest")
    _require(record["record_digest"] == sha256_text(record["text"]),
             f"record {record['record_id']} digest does not match text")
    _require(_is_int(record["valid_from"]) and record["valid_from"] >= 0, "record.valid_from must be non-negative")
    _require(_is_int(record["recorded_at"]) and record["recorded_at"] >= 0, "record.recorded_at must be non-negative")
    if record.get("valid_to") is not None:
        _require(_is_int(record["valid_to"]) and record["valid_to"] > record["valid_from"],
                 "record.valid_to must be after valid_from")
    supersedes = record.get("supersedes", [])
    _require(isinstance(supersedes, list) and len(supersedes) == len(set(supersedes)),
             "record.supersedes must be a duplicate-free list")
    _require(all(isinstance(x, str) and x.strip() for x in supersedes),
             "record.supersedes must contain non-empty strings")


def _validate_expected(expected: Mapping[str, Any]) -> None:
    _require(isinstance(expected, Mapping), "probe.expected must be an object")
    decision = expected.get("decision")
    _require(decision in {"answer", "abstain", "blocked"}, "unsupported expected decision")
    for field in ("required_ids", "forbidden_ids"):
        values = expected.get(field, [])
        _require(isinstance(values, list) and len(values) == len(set(values)),
                 f"expected.{field} must be a duplicate-free list")
        _require(all(isinstance(x, str) and x.strip() for x in values),
                 f"expected.{field} must contain non-empty strings")
    _require(not (set(expected.get("required_ids", [])) & set(expected.get("forbidden_ids", []))),
             "required and forbidden evidence IDs overlap")
    scope = expected.get("required_scope")
    _require(isinstance(scope, str) and scope.strip(), "expected.required_scope is required")
    _require(isinstance(expected.get("require_provenance", True), bool),
             "expected.require_provenance must be boolean")
    max_words = expected.get("max_context_words", 200)
    _require(_is_int(max_words) and 0 <= max_words <= 10000,
             "expected.max_context_words must be bounded")


def _validate_probe_contract(
    case_id: str,
    probe: Mapping[str, Any],
    records_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    expected = probe["expected"]
    required = set(expected.get("required_ids", []))
    forbidden = set(expected.get("forbidden_ids", []))
    known = set(records_by_id)
    _require(required <= known, f"case {case_id} probe references an unknown required record")
    _require(forbidden <= known, f"case {case_id} probe references an unknown forbidden record")
    _require(expected["required_scope"] == probe["scope"],
             f"case {case_id} probe required_scope must match probe scope")
    if required and expected["decision"] == "answer":
        _require(probe["limit"] >= len(required),
                 f"case {case_id} probe limit is smaller than required evidence")
        _require(expected["max_context_words"] > 0,
                 f"case {case_id} answer probe needs a positive context budget")
        _require(
            all(records_by_id[record_id]["scope"] == probe["scope"] for record_id in required),
            f"case {case_id} required evidence must be in the probe scope",
        )


def validate_case_bundle(bundle: Mapping[str, Any], *, max_cases: int = 100) -> None:
    _require(isinstance(bundle, Mapping), "case bundle must be an object")
    _require(bundle.get("schema") == CASE_SCHEMA, "unsupported case bundle schema")
    cases = bundle.get("cases")
    _require(isinstance(cases, list) and cases, "case bundle cases must be non-empty")
    _require(len(cases) <= max_cases, "case bundle exceeds maximum case count")
    case_ids: list[str] = []
    for case in cases:
        _require(isinstance(case, Mapping), "case must be an object")
        case_id = case.get("case_id")
        category = case.get("category")
        _require(isinstance(case_id, str) and case_id.strip(), "case_id is required")
        _require(isinstance(category, str) and category.strip(), f"case {case_id} category is required")
        case_ids.append(case_id)
        events = case.get("events")
        probes = case.get("probes")
        _require(isinstance(events, list), f"case {case_id} events must be a list")
        _require(isinstance(probes, list) and probes, f"case {case_id} probes must be non-empty")
        records_by_id: dict[str, Mapping[str, Any]] = {}
        for event in events:
            _require(isinstance(event, Mapping), f"case {case_id} event must be an object")
            event_type = event.get("type")
            _require(event_type in {"ingest", "forget"}, f"case {case_id} has unsupported event type")
            if event_type == "ingest":
                record = event.get("record", {})
                _validate_record(record)
                records_by_id[str(record["record_id"])] = record
                if "expected_status" in event:
                    expected_status = event["expected_status"]
                    if isinstance(expected_status, str):
                        statuses = [expected_status]
                    else:
                        _require(
                            isinstance(expected_status, list)
                            and len(expected_status) == len(set(expected_status)),
                            "expected_status must be a string or duplicate-free list",
                        )
                        statuses = expected_status
                    _require(
                        statuses and all(status in {"admitted", "quarantined", "rejected"} for status in statuses),
                        "unsupported expected admission status",
                    )
            else:
                _require(isinstance(event.get("record_id"), str) and event["record_id"].strip(),
                         "forget.record_id is required")
                _require(isinstance(event.get("scope"), str) and event["scope"].strip(),
                         "forget.scope is required")
        probe_ids: list[str] = []
        for probe in probes:
            _require(isinstance(probe, Mapping), f"case {case_id} probe must be an object")
            probe_id = probe.get("probe_id")
            _require(isinstance(probe_id, str) and probe_id.strip(), "probe_id is required")
            probe_ids.append(probe_id)
            _require(isinstance(probe.get("query"), str) and probe["query"].strip(),
                     f"probe {probe_id} query is required")
            _require(isinstance(probe.get("scope"), str) and probe["scope"].strip(),
                     f"probe {probe_id} scope is required")
            _require(_is_int(probe.get("as_of")) and probe["as_of"] >= 0, "probe.as_of must be non-negative")
            _require(_is_int(probe.get("limit")) and 1 <= probe["limit"] <= 100, "probe.limit must be 1..100")
            _validate_expected(probe.get("expected", {}))
            _validate_probe_contract(case_id, probe, records_by_id)
        _require(len(probe_ids) == len(set(probe_ids)), f"case {case_id} probe IDs must be unique")
    _require(len(case_ids) == len(set(case_ids)), "case IDs must be unique")


# These are deliberately explicit. Raw text is allowed in the private fixture,
# never in a public run return or acceptance artifact.
_DROP_FIELDS = {
    "query", "text", "body", "body_json", "record_body", "memory_body", "prompt",
    "response", "final_answer", "generated_output", "tool_arguments", "raw", "content",
    "provider_payload", "request_payload", "context",
}
_SECRET_FIELDS = {
    "password", "passwd", "secret", "api_key", "apikey", "access_token",
    "authorization", "bearer_token", "private_key", "client_secret", "token",
}
_ALLOWED_FIELDS = {
    "schema", "suite_id", "run_id", "provider", "provider_contract", "status", "verdict",
    "provider_metadata", "real_producer", "offline", "network_calls", "binary_sha256", "binary_version",
    "acceptance_status", "release_ready", "manifest_sha256", "case_file_sha256",
    "signature_sha256", "source_report_signature", "generated_at", "started_at", "finished_at",
    "case_count", "probe_count", "event_count", "passed_cases", "passed_probes", "passed_checks",
    "required_categories", "categories", "case_results", "case_id", "category", "case_commitment",
    "observations", "probe_id", "passed", "disposition", "expected_decision", "observed_decision",
    "reason_codes", "required_present", "forbidden_present", "scope_ok", "provenance_ok",
    "budget_ok", "hit_count", "context_words", "admissions", "record_id", "admission_status",
    "expected_status", "serveable", "capabilities", "checks", "metrics", "numerator", "denominator", "error_class",
    "errors", "available", "missing_categories", "blocked_cases", "failed_cases", "provider_name",
}


def sanitize_public_projection(value: Any, *, _path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in value.items():
            _require(isinstance(key, str), "public projection keys must be strings")
            key_l = key.casefold()
            if key_l in _SECRET_FIELDS:
                raise ProtocolError(f"secret field {key} cannot cross public boundary")
            if key_l in _DROP_FIELDS:
                continue
            if key not in _ALLOWED_FIELDS and (_path[-1:] not in (("metrics",), ("capabilities",), ("checks",), ("provider_metadata",))):
                raise ProtocolError(f"unknown public field {'.'.join(_path + (key,))}")
            out[key] = sanitize_public_projection(child, _path=_path + (key,))
        return out
    if isinstance(value, list):
        return [sanitize_public_projection(child, _path=_path) for child in value]
    if isinstance(value, float):
        _require(math.isfinite(value), "non-finite number in public projection")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ProtocolError(f"unsupported public projection value at {'.'.join(_path)}")


def signature_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("signature_sha256", None)
    payload.pop("generated_at", None)
    payload.pop("started_at", None)
    payload.pop("finished_at", None)
    return payload


def content_signature(value: Mapping[str, Any]) -> str:
    return sha256_text(canonical_json(signature_payload(value)))
