"""Provider-free synthetic GovCon handling-profile corpus contract.

This module is a benchmark policy fixture, not a legal classifier.  It exercises
an explicit boundary between a synthetic candidate's combined agent-visible
projection, deterministic local redaction, protected storage, and review/block
outcomes.  Receipts contain commitments and bounded labels only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

HANDLING_CORPUS_SCHEMA_VERSION = "perseus-vault-handling-profile-corpus/v1"
HANDLING_PROFILE_SCHEMA_VERSION = "perseus-vault-handling-profile/v1"
_PROVIDER_MODE = "zero-model-zero-network"
HANDLING_PROFILES = (
    "PUBLIC_SAFE",
    "INTERNAL_PROGRAM",
    "FCI_LIKE",
    "CUI_LIKE",
    "EXPORT_CONTROLLED_SIGNAL",
    "CREDENTIAL",
    "REVIEW_REQUIRED",
)
OUTCOMES = (
    "SAVE/AGENT_VISIBLE",
    "PROTECTED",
    "BLOCK",
    "PENDING_REVIEW",
    "REVIEW_REQUIRED",
)
_INPUT_STATES = ("complete", "ambiguous", "provider-unavailable", "malformed")
_REDACTION_MODES = ("none", "permitted", "incomplete")
_CANDIDATE_FIELDS = frozenset(
    {
        "content",
        "title",
        "safe_summary",
        "core_tags",
        "project",
        "task",
        "topic",
        "source_refs",
        "contract_id",
        "program_id",
        "workspace",
        "scope",
    }
)
_CASE_FIELDS = frozenset(
    {
        "case_id",
        "workspace",
        "scope",
        "input_state",
        "redaction_mode",
        "redaction_exclude_fields",
        "expected_profile",
        "expected_outcome",
        "candidate",
    }
)
_CORPUS_FIELDS = frozenset(
    {
        "schema_version",
        "corpus_version",
        "policy_version",
        "taxonomy_version",
        "provider_mode",
        "cases",
    }
)
_CASE_RECEIPT_FIELDS = frozenset(
    {
        "case_id",
        "workspace_sha256",
        "scope_sha256",
        "input_state",
        "expected_profile",
        "classified_profile",
        "actual_profile",
        "expected_outcome",
        "actual_outcome",
        "decision_state",
        "reason_code",
        "redaction_mode",
        "redaction_complete",
        "redaction_sha256",
        "projection_sha256",
        "protected_storage_sha256",
        "candidate_sha256",
        "false_negative",
        "false_positive",
        "mismatch",
        "missingness",
        "policy_version",
        "taxonomy_version",
        "provider_mode",
        "network_calls",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

# Detection is intentionally conservative and synthetic-marker oriented.  The
# labels are policy fixture classes, never legal determinations.
_CLASSIFIERS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "CREDENTIAL",
        "credential-signal",
        re.compile(r"(?:credential[-_ ]synthetic|api[_ -]?key[-_ ]?synthetic|password[-_ ]?synthetic|bearer[-_ ]?synthetic|secret[-_ ]?synthetic)", re.I),
    ),
    (
        "EXPORT_CONTROLLED_SIGNAL",
        "export-controlled-signal",
        re.compile(r"(?:export[-_ ]controlled|export[-_ ]controlled[-_ ]synthetic|\bitar\b|\bear\b|usml)", re.I),
    ),
    (
        "CUI_LIKE",
        "cui-signal",
        re.compile(r"(?:\bcui\b|controlled[-_ ]unclassified|cui[-_ ]synthetic)", re.I),
    ),
    (
        "FCI_LIKE",
        "fci-signal",
        re.compile(r"(?:\bfci\b|fci[-_ ]synthetic|federal[-_ ]contract[-_ ]information)", re.I),
    ),
    (
        "INTERNAL_PROGRAM",
        "internal-program-signal",
        re.compile(r"(?:internal[-_ ]program|internal[-_ ]program[-_ ]synthetic)", re.I),
    ),
)
_REDACTION_PATTERNS = (
    re.compile(r"credential[-_ ]synthetic[-_ ]?[A-Za-z0-9_-]*", re.I),
    re.compile(r"api[_ -]?key[-_ ]?synthetic[-_ ]?[A-Za-z0-9_-]*", re.I),
    re.compile(r"cui[-_ ]synthetic[-_ ]?[A-Za-z0-9_-]*", re.I),
    re.compile(r"fci[-_ ]synthetic[-_ ]?[A-Za-z0-9_-]*", re.I),
    re.compile(r"export[-_ ]controlled[-_ ]synthetic[-_ ]?[A-Za-z0-9_-]*", re.I),
)


class HandlingProfileError(ValueError):
    """Raised when the synthetic handling contract fails closed."""


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HandlingProfileError("value is not canonical JSON") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise HandlingProfileError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _text(value: Any, field: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise HandlingProfileError(f"{field} must be bounded text")
    if identifier and not _IDENTIFIER.fullmatch(value):
        raise HandlingProfileError(f"{field} is not a bounded identifier")
    return value


def _keys(value: Any, expected: frozenset[str], field: str) -> None:
    if not isinstance(value, Mapping):
        raise HandlingProfileError(f"{field} must be an object")
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise HandlingProfileError(f"{field} missing field: {sorted(missing)[0]}")
    if unknown:
        raise HandlingProfileError(f"{field} contains unknown field: {sorted(unknown)[0]}")


def _unique_text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HandlingProfileError(f"{field} must be a list of text")
    if len(value) != len(set(value)):
        raise HandlingProfileError(f"{field} must not contain duplicates")
    return list(value)


def _validate_source_refs(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise HandlingProfileError(f"{field} must be a list")
    for index, row in enumerate(value):
        _keys(row, frozenset({"label", "uri"}), f"{field}[{index}]")
        _text(row["label"], f"{field}[{index}].label", identifier=True)
        _text(row["uri"], f"{field}[{index}].uri", identifier=True)


def _validate_candidate(candidate: Any, *, allow_missing: bool) -> None:
    if not isinstance(candidate, Mapping):
        raise HandlingProfileError("candidate must be an object")
    unknown = set(candidate) - _CANDIDATE_FIELDS
    if unknown:
        raise HandlingProfileError(f"candidate contains unknown field: {sorted(unknown)[0]}")
    required = _CANDIDATE_FIELDS if not allow_missing else set(candidate)
    missing = _CANDIDATE_FIELDS - set(candidate)
    if missing and not allow_missing:
        raise HandlingProfileError(f"candidate missing field: {sorted(missing)[0]}")
    for field in ("content", "title", "safe_summary", "project", "task", "topic", "contract_id", "program_id", "workspace", "scope"):
        if field in required:
            _text(candidate.get(field), f"candidate.{field}")
    if "core_tags" in required:
        _unique_text_list(candidate.get("core_tags"), "candidate.core_tags")
    if "source_refs" in required:
        _validate_source_refs(candidate.get("source_refs"), "candidate.source_refs")


def validate_handling_corpus(corpus: Any) -> None:
    _keys(corpus, _CORPUS_FIELDS, "corpus")
    if corpus["schema_version"] != HANDLING_CORPUS_SCHEMA_VERSION:
        raise HandlingProfileError("unsupported handling corpus schema")
    for field in ("corpus_version", "policy_version", "taxonomy_version"):
        _text(corpus[field], field, identifier=True)
    if corpus["provider_mode"] != _PROVIDER_MODE:
        raise HandlingProfileError("default handling corpus must be zero-model-zero-network")
    cases = corpus["cases"]
    if not isinstance(cases, list) or not cases:
        raise HandlingProfileError("corpus cases must be a non-empty list")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        _keys(case, _CASE_FIELDS, f"cases[{index}]")
        case_id = _text(case["case_id"], f"cases[{index}].case_id", identifier=True)
        if case_id in seen:
            raise HandlingProfileError("case IDs must be unique")
        seen.add(case_id)
        _text(case["workspace"], f"cases[{index}].workspace", identifier=True)
        _text(case["scope"], f"cases[{index}].scope", identifier=True)
        if case["input_state"] not in _INPUT_STATES:
            raise HandlingProfileError(f"cases[{index}].input_state is invalid")
        if case["redaction_mode"] not in _REDACTION_MODES:
            raise HandlingProfileError(f"cases[{index}].redaction_mode is invalid")
        excluded = _unique_text_list(case["redaction_exclude_fields"], f"cases[{index}].redaction_exclude_fields")
        if any(field not in _CANDIDATE_FIELDS for field in excluded):
            raise HandlingProfileError(f"cases[{index}].redaction_exclude_fields contains an unknown field")
        if case["expected_profile"] not in HANDLING_PROFILES:
            raise HandlingProfileError(f"cases[{index}].expected_profile is invalid")
        if case["expected_outcome"] not in OUTCOMES:
            raise HandlingProfileError(f"cases[{index}].expected_outcome is invalid")
        _validate_candidate(case["candidate"], allow_missing=case["input_state"] == "malformed")
        candidate = case["candidate"]
        if "workspace" in candidate and candidate["workspace"] != case["workspace"]:
            raise HandlingProfileError(f"cases[{index}] workspace does not bind candidate workspace")
        if "scope" in candidate and candidate["scope"] != case["scope"]:
            raise HandlingProfileError(f"cases[{index}] scope does not bind candidate scope")


def classify_candidate(candidate: Mapping[str, Any]) -> dict[str, str]:
    """Classify one complete candidate using the combined projection surface."""
    _validate_candidate(candidate, allow_missing=False)
    lowered = stable_json(candidate).lower()
    for profile, reason, pattern in _CLASSIFIERS:
        if pattern.search(lowered):
            return {"profile": profile, "reason_code": reason}
    if "ambiguous-signal" in lowered:
        return {"profile": "REVIEW_REQUIRED", "reason_code": "ambiguous-signal"}
    return {"profile": "PUBLIC_SAFE", "reason_code": "no-sensitive-signal"}


def _redact_value(value: Any, *, excluded: frozenset[str], root_field: str) -> tuple[Any, bool]:
    if root_field in excluded:
        return copy.deepcopy(value), False
    if isinstance(value, str):
        result = value
        changed = False
        for pattern in _REDACTION_PATTERNS:
            result, count = pattern.subn("[REDACTED-SYNTHETIC-SIGNAL]", result)
            changed = changed or count > 0
        return result, changed
    if isinstance(value, list):
        out_list: list[Any] = []
        complete = True
        for child in value:
            redacted, child_complete = _redact_value(child, excluded=excluded, root_field=root_field)
            out_list.append(redacted)
            complete = complete and child_complete
        return out_list, complete
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        complete = True
        for key, child in value.items():
            redacted, child_complete = _redact_value(child, excluded=excluded, root_field=root_field)
            out[key] = redacted
            complete = complete and child_complete
        return out, complete
    return copy.deepcopy(value), True


def redact_candidate(candidate: Mapping[str, Any], *, exclude_fields: Sequence[str] = ()) -> tuple[dict[str, Any], bool]:
    """Apply deterministic synthetic-marker redaction and reclassification readiness."""
    _validate_candidate(candidate, allow_missing=False)
    excluded = frozenset(exclude_fields)
    if any(field not in _CANDIDATE_FIELDS for field in excluded):
        raise HandlingProfileError("redaction exclusion contains an unknown candidate field")
    output: dict[str, Any] = {}
    for key, value in candidate.items():
        redacted, _ = _redact_value(value, excluded=excluded, root_field=key)
        output[key] = redacted
    complete = classify_candidate(output)["profile"] == "PUBLIC_SAFE"
    return output, complete


def _case_receipt(case: Mapping[str, Any], corpus: Mapping[str, Any]) -> dict[str, Any]:
    candidate = case["candidate"]
    candidate_digest = sha256_text(stable_json(candidate))
    workspace_sha = sha256_text(case["workspace"])
    scope_sha = sha256_text(case["scope"])
    input_state = case["input_state"]
    redaction_mode = case["redaction_mode"]
    redaction_complete = redaction_mode == "none"
    redacted_digest: str | None = None
    classified_profile = "REVIEW_REQUIRED"
    actual_profile = "REVIEW_REQUIRED"
    actual_outcome = "REVIEW_REQUIRED"
    decision_state = "REVIEW_REQUIRED"
    reason_code = "review-required"
    if input_state == "provider-unavailable":
        reason_code = "provider-unavailable"
    elif input_state == "ambiguous":
        actual_outcome, decision_state = "PENDING_REVIEW", "PENDING_REVIEW"
        reason_code = "ambiguous-input"
    elif input_state == "malformed":
        reason_code = "malformed-metadata"
    else:
        classification = classify_candidate(candidate)
        classified_profile = classification["profile"]
        actual_profile = classified_profile
        reason_code = classification["reason_code"]
        if classified_profile == "CREDENTIAL":
            actual_outcome, decision_state, reason_code = "BLOCK", "BLOCK", "credential-block"
        elif classified_profile == "PUBLIC_SAFE":
            actual_outcome, decision_state = "SAVE/AGENT_VISIBLE", "SAVE"
        elif redaction_mode in {"permitted", "incomplete"}:
            redacted, redaction_complete = redact_candidate(
                candidate,
                exclude_fields=case["redaction_exclude_fields"],
            )
            redacted_digest = sha256_text(stable_json(redacted))
            if redaction_complete and redaction_mode == "permitted" and classified_profile != "CREDENTIAL":
                actual_profile = "PUBLIC_SAFE"
                actual_outcome, decision_state = "SAVE/AGENT_VISIBLE", "SAVE"
                reason_code = "redaction-complete-agent-visible"
            else:
                actual_outcome, decision_state = "REVIEW_REQUIRED", "REVIEW_REQUIRED"
                reason_code = "incomplete-redaction"
        else:
            actual_outcome, decision_state = "PROTECTED", "PROTECTED"
            reason_code = f"{classified_profile.lower()}-protected"
    if input_state == "ambiguous":
        actual_profile = "REVIEW_REQUIRED"
        actual_outcome, decision_state = "PENDING_REVIEW", "PENDING_REVIEW"
    elif input_state != "complete":
        actual_profile = "REVIEW_REQUIRED"
        actual_outcome, decision_state = "REVIEW_REQUIRED", "REVIEW_REQUIRED"
    redaction_meta = {
        "mode": redaction_mode,
        "complete": redaction_complete,
        "excluded_fields": sorted(case["redaction_exclude_fields"]),
        "redacted_candidate_sha256": redacted_digest,
    }
    redaction_digest = sha256_text(stable_json(redaction_meta))
    visible = actual_outcome == "SAVE/AGENT_VISIBLE"
    projection_digest = sha256_text(
        stable_json(
            {
                "visible": visible,
                "workspace_sha256": workspace_sha,
                "candidate_sha256": redacted_digest if visible and redacted_digest else candidate_digest if visible else None,
            }
        )
    )
    protected_digest = candidate_digest if actual_outcome in {"PROTECTED", "REVIEW_REQUIRED"} else None
    expected_outcome = case["expected_outcome"]
    false_negative = expected_outcome != "SAVE/AGENT_VISIBLE" and visible
    false_positive = expected_outcome == "SAVE/AGENT_VISIBLE" and not visible
    missingness = input_state != "complete" or (redaction_mode == "incomplete" and not redaction_complete)
    mismatch = expected_outcome != actual_outcome
    return {
        "case_id": case["case_id"],
        "workspace_sha256": workspace_sha,
        "scope_sha256": scope_sha,
        "input_state": input_state,
        "expected_profile": case["expected_profile"],
        "classified_profile": classified_profile,
        "actual_profile": actual_profile,
        "expected_outcome": expected_outcome,
        "actual_outcome": actual_outcome,
        "decision_state": decision_state,
        "reason_code": reason_code,
        "redaction_mode": redaction_mode,
        "redaction_complete": redaction_complete,
        "redaction_sha256": redaction_digest,
        "projection_sha256": projection_digest,
        "protected_storage_sha256": protected_digest,
        "candidate_sha256": candidate_digest,
        "false_negative": false_negative,
        "false_positive": false_positive,
        "mismatch": mismatch,
        "missingness": missingness,
        "policy_version": corpus["policy_version"],
        "taxonomy_version": corpus["taxonomy_version"],
        "provider_mode": _PROVIDER_MODE,
        "network_calls": 0,
    }


def build_handling_profile_report(corpus: Mapping[str, Any]) -> dict[str, Any]:
    """Run the synthetic handling corpus and return a sealed public receipt."""
    validate_handling_corpus(corpus)
    rows = [_case_receipt(case, corpus) for case in corpus["cases"]]
    outcome_counts = {outcome: sum(row["actual_outcome"] == outcome for row in rows) for outcome in OUTCOMES}
    expected_counts = {profile: sum(row["expected_profile"] == profile for row in rows) for profile in HANDLING_PROFILES}
    actual_counts = {profile: sum(row["actual_profile"] == profile for row in rows) for profile in HANDLING_PROFILES}
    metrics: dict[str, dict[str, int]] = {}
    for profile in HANDLING_PROFILES:
        subset = [row for row in rows if row["expected_profile"] == profile]
        metrics[profile] = {
            "expected_count": len(subset),
            "classified_count": sum(row["classified_profile"] == profile for row in subset),
            "actual_count": sum(row["actual_profile"] == profile for row in subset),
            "false_negative_count": sum(row["false_negative"] for row in subset),
            "false_positive_count": sum(row["false_positive"] for row in subset),
            "mismatch_count": sum(row["mismatch"] for row in subset),
            "missingness_count": sum(row["missingness"] for row in subset),
        }
    visible_rows = [row for row in rows if row["actual_outcome"] == "SAVE/AGENT_VISIBLE"]
    protected_rows = [row for row in rows if row["actual_outcome"] != "SAVE/AGENT_VISIBLE"]
    base: dict[str, Any] = {
        "schema_version": HANDLING_PROFILE_SCHEMA_VERSION,
        "corpus_version": corpus["corpus_version"],
        "policy_version": corpus["policy_version"],
        "taxonomy_version": corpus["taxonomy_version"],
        "provider_mode": _PROVIDER_MODE,
        "provider_calls": 0,
        "network_calls": 0,
        "raw_inputs_captured": False,
        "case_count": len(rows),
        "cases": rows,
        "expected_profile_counts": expected_counts,
        "actual_profile_counts": actual_counts,
        "outcome_counts": outcome_counts,
        "metrics_by_profile": metrics,
        "totals": {
            "false_negative_count": sum(row["false_negative"] for row in rows),
            "false_positive_count": sum(row["false_positive"] for row in rows),
            "mismatch_count": sum(row["mismatch"] for row in rows),
            "missingness_count": sum(row["missingness"] for row in rows),
        },
        "scope_isolation": {
            "workspace_count": len({row["workspace_sha256"] for row in rows}),
            "agent_visible_case_count": len(visible_rows),
            "protected_case_count": len(protected_rows),
            "protected_recall_exposure_count": 0,
            "redacted_original_exposure_count": 0,
        },
    }
    digest = sha256_text(stable_json(base))
    report = dict(base)
    report["report_sha256"] = digest
    report["signature_sha256"] = digest
    validate_handling_report(report)
    return report


def agent_visible_case_ids(report: Mapping[str, Any], workspace: str) -> list[str]:
    """Return only safe projection IDs for one exact workspace partition."""
    validate_handling_report(report)
    workspace_sha = sha256_text(_text(workspace, "workspace", identifier=True))
    return sorted(
        row["case_id"]
        for row in report["cases"]
        if row["workspace_sha256"] == workspace_sha and row["actual_outcome"] == "SAVE/AGENT_VISIBLE"
    )


def validate_handling_report(report: Any) -> None:
    if not isinstance(report, Mapping):
        raise HandlingProfileError("handling report must be an object")
    required = {
        "schema_version", "corpus_version", "policy_version", "taxonomy_version", "provider_mode",
        "provider_calls", "network_calls", "raw_inputs_captured", "case_count", "cases",
        "expected_profile_counts", "actual_profile_counts", "outcome_counts", "metrics_by_profile",
        "totals", "scope_isolation", "report_sha256", "signature_sha256",
    }
    _keys(report, frozenset(required), "report")
    if report["schema_version"] != HANDLING_PROFILE_SCHEMA_VERSION:
        raise HandlingProfileError("unsupported handling report schema")
    for field in ("corpus_version", "policy_version", "taxonomy_version"):
        _text(report[field], field, identifier=True)
    if report["provider_mode"] != _PROVIDER_MODE or report["provider_calls"] != 0 or report["network_calls"] != 0:
        raise HandlingProfileError("handling report must be provider-free and network-free")
    if report["raw_inputs_captured"] is not False:
        raise HandlingProfileError("handling report must declare raw_inputs_captured=false")
    if isinstance(report["case_count"], bool) or not isinstance(report["case_count"], int) or report["case_count"] < 1:
        raise HandlingProfileError("case_count must be positive")
    rows = report["cases"]
    if not isinstance(rows, list) or len(rows) != report["case_count"]:
        raise HandlingProfileError("case rows do not match case_count")
    seen: set[str] = set()
    for index, row in enumerate(rows):
        _keys(row, _CASE_RECEIPT_FIELDS, f"cases[{index}]")
        case_id = _text(row["case_id"], f"cases[{index}].case_id", identifier=True)
        if case_id in seen:
            raise HandlingProfileError("report case IDs must be unique")
        seen.add(case_id)
        for field in ("workspace_sha256", "scope_sha256", "redaction_sha256", "projection_sha256", "candidate_sha256"):
            _sha(row[field], f"cases[{index}].{field}")
        if row["protected_storage_sha256"] is not None:
            _sha(row["protected_storage_sha256"], f"cases[{index}].protected_storage_sha256")
        for field in ("expected_profile", "classified_profile", "actual_profile"):
            if row[field] not in HANDLING_PROFILES:
                raise HandlingProfileError(f"cases[{index}].{field} is invalid")
        for field in ("expected_outcome", "actual_outcome"):
            if row[field] not in OUTCOMES:
                raise HandlingProfileError(f"cases[{index}].{field} is invalid")
        if row["input_state"] not in _INPUT_STATES or row["redaction_mode"] not in _REDACTION_MODES:
            raise HandlingProfileError(f"cases[{index}] state is invalid")
        if row["provider_mode"] != _PROVIDER_MODE or row["network_calls"] != 0:
            raise HandlingProfileError(f"cases[{index}] provider/network mode is invalid")
        for field in ("redaction_complete", "false_negative", "false_positive", "mismatch", "missingness"):
            if not isinstance(row[field], bool):
                raise HandlingProfileError(f"cases[{index}].{field} must be boolean")
        expected_state = {
            "SAVE/AGENT_VISIBLE": "SAVE",
            "PROTECTED": "PROTECTED",
            "BLOCK": "BLOCK",
            "PENDING_REVIEW": "PENDING_REVIEW",
            "REVIEW_REQUIRED": "REVIEW_REQUIRED",
        }[row["actual_outcome"]]
        if row["decision_state"] != expected_state:
            raise HandlingProfileError(f"cases[{index}] decision state contradicts actual outcome")
        if row["mismatch"] != (row["expected_outcome"] != row["actual_outcome"]):
            raise HandlingProfileError(f"cases[{index}] mismatch flag is inconsistent")
        if row["false_negative"] != (row["expected_outcome"] != "SAVE/AGENT_VISIBLE" and row["actual_outcome"] == "SAVE/AGENT_VISIBLE"):
            raise HandlingProfileError(f"cases[{index}] false-negative flag is inconsistent")
        if row["false_positive"] != (row["expected_outcome"] == "SAVE/AGENT_VISIBLE" and row["actual_outcome"] != "SAVE/AGENT_VISIBLE"):
            raise HandlingProfileError(f"cases[{index}] false-positive flag is inconsistent")
        expected_missingness = row["input_state"] != "complete" or (row["redaction_mode"] == "incomplete" and not row["redaction_complete"])
        if row["missingness"] != expected_missingness:
            raise HandlingProfileError(f"cases[{index}] missingness flag is inconsistent")
        if row["redaction_mode"] == "none" and not row["redaction_complete"]:
            raise HandlingProfileError(f"cases[{index}] no-redaction case cannot be incomplete")
        if row["actual_outcome"] == "SAVE/AGENT_VISIBLE" and row["actual_profile"] != "PUBLIC_SAFE":
            raise HandlingProfileError(f"cases[{index}] agent-visible outcome must have PUBLIC_SAFE actual profile")
        if row["actual_outcome"] == "BLOCK" and row["protected_storage_sha256"] is not None:
            raise HandlingProfileError(f"cases[{index}] blocked content must not claim protected storage")
        if row["actual_outcome"] in {"PROTECTED", "REVIEW_REQUIRED"} and row["protected_storage_sha256"] is None:
            raise HandlingProfileError(f"cases[{index}] protected/review content needs a storage commitment")
    expected_profile_counts = {profile: sum(row["expected_profile"] == profile for row in rows) for profile in HANDLING_PROFILES}
    actual_profile_counts = {profile: sum(row["actual_profile"] == profile for row in rows) for profile in HANDLING_PROFILES}
    outcome_counts = {outcome: sum(row["actual_outcome"] == outcome for row in rows) for outcome in OUTCOMES}
    if dict(report["expected_profile_counts"]) != expected_profile_counts:
        raise HandlingProfileError("expected profile counts do not match case rows")
    if dict(report["actual_profile_counts"]) != actual_profile_counts:
        raise HandlingProfileError("actual profile counts do not match case rows")
    if dict(report["outcome_counts"]) != outcome_counts:
        raise HandlingProfileError("outcome counts do not match case rows")
    for field, allowed in (("expected_profile_counts", HANDLING_PROFILES), ("actual_profile_counts", HANDLING_PROFILES), ("outcome_counts", OUTCOMES)):
        value = report[field]
        if not isinstance(value, Mapping) or set(value) != set(allowed):
            raise HandlingProfileError(f"{field} is incomplete")
        if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in value.values()):
            raise HandlingProfileError(f"{field} contains an invalid count")
        if sum(value.values()) != report["case_count"]:
            raise HandlingProfileError(f"{field} does not sum to case_count")
    metrics = report["metrics_by_profile"]
    if not isinstance(metrics, Mapping) or set(metrics) != set(HANDLING_PROFILES):
        raise HandlingProfileError("metrics_by_profile is incomplete")
    metric_fields = {"expected_count", "classified_count", "actual_count", "false_negative_count", "false_positive_count", "mismatch_count", "missingness_count"}
    for profile, metric in metrics.items():
        _keys(metric, frozenset(metric_fields), f"metrics_by_profile.{profile}")
        if any(isinstance(metric[field], bool) or not isinstance(metric[field], int) or metric[field] < 0 for field in metric_fields):
            raise HandlingProfileError(f"metrics_by_profile.{profile} contains an invalid count")
        subset = [row for row in rows if row["expected_profile"] == profile]
        expected_metric = {
            "expected_count": len(subset),
            "classified_count": sum(row["classified_profile"] == profile for row in subset),
            "actual_count": sum(row["actual_profile"] == profile for row in subset),
            "false_negative_count": sum(row["false_negative"] for row in subset),
            "false_positive_count": sum(row["false_positive"] for row in subset),
            "mismatch_count": sum(row["mismatch"] for row in subset),
            "missingness_count": sum(row["missingness"] for row in subset),
        }
        if dict(metric) != expected_metric:
            raise HandlingProfileError(f"metrics_by_profile.{profile} does not match case rows")
    expected_totals = {
        "false_negative_count": sum(row["false_negative"] for row in rows),
        "false_positive_count": sum(row["false_positive"] for row in rows),
        "mismatch_count": sum(row["mismatch"] for row in rows),
        "missingness_count": sum(row["missingness"] for row in rows),
    }
    _keys(report["totals"], frozenset(expected_totals), "totals")
    if dict(report["totals"]) != expected_totals:
        raise HandlingProfileError("totals do not match case rows")
    expected_scope = {
        "workspace_count": len({row["workspace_sha256"] for row in rows}),
        "agent_visible_case_count": sum(row["actual_outcome"] == "SAVE/AGENT_VISIBLE" for row in rows),
        "protected_case_count": sum(row["actual_outcome"] != "SAVE/AGENT_VISIBLE" for row in rows),
        "protected_recall_exposure_count": 0,
        "redacted_original_exposure_count": 0,
    }
    _keys(report["scope_isolation"], frozenset(expected_scope), "scope_isolation")
    if dict(report["scope_isolation"]) != expected_scope:
        raise HandlingProfileError("scope isolation aggregate does not match case rows")
    for value in list(report["totals"].values()) + list(report["scope_isolation"].values()):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HandlingProfileError("report aggregate contains an invalid count")
    base = {key: value for key, value in report.items() if key not in {"report_sha256", "signature_sha256"}}
    expected = sha256_text(stable_json(base))
    _sha(report["report_sha256"], "report_sha256")
    _sha(report["signature_sha256"], "signature_sha256")
    if report["report_sha256"] != expected or report["signature_sha256"] != expected:
        raise HandlingProfileError("handling report digest mismatch")
