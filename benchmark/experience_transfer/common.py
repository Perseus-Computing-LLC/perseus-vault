"""Shared contracts for the provider-free verified-experience-transfer benchmark."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

CORPUS_SCHEMA = "verified-experience-transfer-corpus/v1"
MANIFEST_SCHEMA = "verified-experience-transfer-manifest/v1"
REPORT_SCHEMA = "verified-experience-transfer-public-report/v1"
SHARED_VIEWS_SCHEMA = "verified-experience-transfer-shared-agent-views/v1"
ACCEPTANCE_SCHEMA = "verified-experience-transfer-acceptance/v1"
ADAPTER_CONTRACT_VERSION = "verified-experience-transfer-adapter/v1"
ALLOWED_DECISIONS = ("reuse", "reject", "abstain", "block")
ALLOWED_STATUSES = ("pass", "fail", "not_measured", "blocked")
REQUIRED_CATEGORIES = (
    "successful_reuse",
    "failed_approach",
    "stale_repository_world",
    "contradiction_supersession",
    "deleted_revoked_evidence",
    "derived_memory_contamination",
    "authority_change",
    "split_brain",
    "insufficient_evidence",
    "revalidation_required",
)
EXPECTED_CATEGORY_COUNTS = {
    "successful_reuse": 3,
    "failed_approach": 3,
    "stale_repository_world": 3,
    "contradiction_supersession": 3,
    "deleted_revoked_evidence": 2,
    "derived_memory_contamination": 2,
    "authority_change": 2,
    "split_brain": 2,
    "insufficient_evidence": 2,
    "revalidation_required": 2,
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
ISO_UTC = re.compile(r"^2026-08-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FORBIDDEN_PUBLIC_KEYS = {
    "prompt",
    "prompts",
    "context",
    "context_body",
    "body",
    "body_json",
    "response",
    "provider_response",
    "raw_payload",
    "tool_arguments",
    "query",
    "credential",
    "credentials",
    "secret",
    "secrets",
    "password",
    "authorization",
    "api_key",
    "access_token",
    "token_value",
}
FORBIDDEN_PUBLIC_SUFFIXES = ("_prompt", "_body", "_response", "_secret", "_credential")
ALLOWED_PUBLIC_CASE_KEYS = {
    "case_id",
    "adapter",
    "decision",
    "reason_code",
    "decision_match",
    "negative_control",
    "revalidated",
    "provenance_validated",
    "authority_checked",
    "unsafe_reuse",
    "selected_memory_count",
    "transition_steps",
}
ALLOWED_PUBLIC_METRIC_KEYS = {
    "metric",
    "status",
    "numerator",
    "denominator",
    "rate",
    "polarity",
    "scope",
}


class ContractError(ValueError):
    """Raised when a benchmark contract cannot be verified."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise ContractError(f"{name} must be a lowercase SHA-256 digest")
    return value


def require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
        raise ContractError(f"{name} must be a bounded identifier")
    return value


def require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be boolean")
    return value


def require_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a non-negative integer")
    return value


def require_rate(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ContractError(f"{name} must be a finite rate in [0, 1]")
    return float(value)


def require_utc(value: Any, name: str) -> str:
    if not isinstance(value, str) or ISO_UTC.fullmatch(value) is None:
        raise ContractError(f"{name} must use the pinned UTC fixture format")
    return value


def reject_forbidden(value: Any, *, path: str = "$") -> None:
    """Reject private/raw fields in a public projection; never silently redact."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in FORBIDDEN_PUBLIC_KEYS or lowered.endswith(FORBIDDEN_PUBLIC_SUFFIXES):
                raise ContractError(f"forbidden public field at {path}.{key}")
            reject_forbidden(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_forbidden(child, path=f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ContractError(f"non-finite public number at {path}")


def source_commitment(case_id: str, source: Mapping[str, Any]) -> str:
    payload = {
        "case_id": case_id,
        "source_id": source.get("source_id"),
        "scope": source.get("scope"),
        "capture_mode": source.get("capture_mode"),
        "captured_at": source.get("captured_at"),
    }
    return canonical_digest(payload)


def evidence_commitment(case_id: str, evidence: Mapping[str, Any]) -> str:
    payload = {
        "case_id": case_id,
        "evidence_id": evidence.get("evidence_id"),
        "source_id": evidence.get("source_id"),
        "status": evidence.get("status"),
        "valid_from": evidence.get("valid_from"),
        "valid_to": evidence.get("valid_to"),
        "capture_mode": evidence.get("capture_mode"),
    }
    return canonical_digest(payload)


def world_commitment(world: Mapping[str, Any]) -> str:
    return canonical_digest({
        "world_state_id": world.get("world_state_id"),
        "version": world.get("version"),
        "status": world.get("status"),
        "facts_code": world.get("facts_code"),
    })


def authority_commitment(authority: Mapping[str, Any]) -> str:
    return canonical_digest({
        "authority_id": authority.get("authority_id"),
        "captured_version": authority.get("captured_version"),
        "current_version": authority.get("current_version"),
        "status": authority.get("status"),
        "permitted_actions": authority.get("permitted_actions"),
        "effective_at": authority.get("effective_at"),
    })


def lineage_commitment(lineage: Mapping[str, Any], experience_id: str) -> str:
    return canonical_digest({
        "experience_id": experience_id,
        "parent_experience_ids": lineage.get("parent_experience_ids"),
        "derivation_status": lineage.get("derivation_status"),
        "raw_source_status": lineage.get("raw_source_status"),
    })


def revalidation_commitment(revalidation: Mapping[str, Any], experience_id: str) -> str:
    return canonical_digest({
        "experience_id": experience_id,
        "current_world_state_hash": revalidation.get("current_world_state_hash"),
        "result": revalidation.get("result"),
        "checked_at": revalidation.get("checked_at"),
    })


def transition_allowed(previous: str, current: str) -> bool:
    allowed = {
        "captured": {"verified", "failed_verification"},
        "verified": {"promoted", "quarantined"},
        "failed_verification": {"promoted", "quarantined"},
        "promoted": {"transferred"},
        "quarantined": {"transferred"},
        "transferred": {"revalidated", "reused", "rejected", "abstained", "blocked"},
        "revalidated": {"reused", "rejected", "abstained", "blocked"},
    }
    return current in allowed.get(previous, set())


def validate_transition_chain(chain: Any, expected_decision: str, name: str = "transition_chain") -> None:
    if not isinstance(chain, list) or len(chain) < 5:
        raise ContractError(f"{name} must contain the complete lifecycle")
    previous = None
    seen_states: list[str] = []
    for index, item in enumerate(chain):
        if not isinstance(item, Mapping):
            raise ContractError(f"{name}[{index}] is not an object")
        required = {"state", "event_id", "actor", "at", "authorized"}
        if set(item) != required:
            raise ContractError(f"{name}[{index}] shape mismatch")
        state = item["state"]
        require_identifier(state, f"{name}[{index}].state")
        require_identifier(item["event_id"], f"{name}[{index}].event_id")
        require_identifier(item["actor"], f"{name}[{index}].actor")
        require_utc(item["at"], f"{name}[{index}].at")
        require_bool(item["authorized"], f"{name}[{index}].authorized")
        if not item["authorized"]:
            raise ContractError(f"accepted transition {name}[{index}] is unauthorized")
        if previous is not None and not transition_allowed(previous, state):
            raise ContractError(f"invalid transition {previous} -> {state}")
        previous = state
        seen_states.append(state)
    final_state = {"reuse": "reused", "reject": "rejected", "abstain": "abstained", "block": "blocked"}[expected_decision]
    if seen_states[-1] != final_state:
        raise ContractError(f"{name} final state {seen_states[-1]!r} != {final_state!r}")
    if "transferred" not in seen_states:
        raise ContractError(f"{name} omitted transfer state")
    if expected_decision == "reuse" and "revalidated" in seen_states and seen_states.index("revalidated") < seen_states.index("transferred"):
        raise ContractError(f"{name} has invalid revalidation ordering")


def validate_experience(case_id: str, experience: Mapping[str, Any]) -> None:
    required = {
        "experience_id", "memory_id", "event_id", "approach_type", "approach_outcome",
        "approach_code", "capture", "validity", "source", "evidence", "lineage",
    }
    if set(experience) != required:
        raise ContractError(f"{case_id}.experience shape mismatch")
    for field in ("experience_id", "memory_id", "event_id", "approach_code", "approach_type", "approach_outcome"):
        require_identifier(experience[field], f"{case_id}.experience.{field}")
    if experience["approach_type"] not in {"worked", "failed"} or experience["approach_outcome"] not in {"verified_success", "failed"}:
        raise ContractError(f"{case_id}.experience approach vocabulary mismatch")
    capture = experience["capture"]
    if not isinstance(capture, Mapping) or set(capture) != {"captured_at", "valid_from", "valid_to", "capture_mode"}:
        raise ContractError(f"{case_id}.experience.capture shape mismatch")
    for field in ("captured_at", "valid_from"):
        require_utc(capture[field], f"{case_id}.experience.capture.{field}")
    if capture["valid_to"] is not None:
        require_utc(capture["valid_to"], f"{case_id}.experience.capture.valid_to")
    if capture["capture_mode"] not in {"hash_only", "snapshot"}:
        raise ContractError(f"{case_id}.experience.capture.capture_mode invalid")
    validity = experience["validity"]
    if not isinstance(validity, Mapping) or set(validity) != {"status", "world_state_hash", "superseded_by"}:
        raise ContractError(f"{case_id}.experience.validity shape mismatch")
    if validity["status"] not in {"current", "stale", "superseded", "revoked"}:
        raise ContractError(f"{case_id}.experience.validity.status invalid")
    require_digest(validity["world_state_hash"], f"{case_id}.experience.validity.world_state_hash")
    if validity["superseded_by"] is not None:
        require_identifier(validity["superseded_by"], f"{case_id}.experience.validity.superseded_by")
    source = experience["source"]
    if not isinstance(source, Mapping) or set(source) != {"source_id", "commitment", "scope", "capture_mode", "captured_at"}:
        raise ContractError(f"{case_id}.experience.source shape mismatch")
    require_identifier(source["source_id"], f"{case_id}.experience.source.source_id")
    require_identifier(source["scope"], f"{case_id}.experience.source.scope")
    require_digest(source["commitment"], f"{case_id}.experience.source.commitment")
    require_utc(source["captured_at"], f"{case_id}.experience.source.captured_at")
    if source["capture_mode"] not in {"hash_only", "snapshot"}:
        raise ContractError(f"{case_id}.experience.source.capture_mode invalid")
    if source["commitment"] != source_commitment(case_id, source):
        raise ContractError(f"{case_id}.experience.source commitment mismatch")
    evidence = experience["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise ContractError(f"{case_id}.experience.evidence must be non-empty")
    evidence_ids: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise ContractError(f"{case_id}.experience.evidence[{index}] is not an object")
        required_evidence = {"evidence_id", "source_id", "commitment", "status", "valid_from", "valid_to", "capture_mode", "quality"}
        if set(item) != required_evidence:
            raise ContractError(f"{case_id}.experience.evidence[{index}] shape mismatch")
        require_identifier(item["evidence_id"], f"{case_id}.experience.evidence[{index}].evidence_id")
        if item["evidence_id"] in evidence_ids:
            raise ContractError(f"{case_id}.experience duplicate evidence id")
        evidence_ids.add(item["evidence_id"])
        if item["source_id"] != source["source_id"]:
            raise ContractError(f"{case_id}.experience evidence source mismatch")
        require_digest(item["commitment"], f"{case_id}.experience.evidence[{index}].commitment")
        if item["status"] not in {"verified", "missing", "insufficient", "revoked", "deleted", "tampered", "superseded"}:
            raise ContractError(f"{case_id}.experience.evidence[{index}].status invalid")
        for field in ("valid_from",):
            require_utc(item[field], f"{case_id}.experience.evidence[{index}].{field}")
        if item["valid_to"] is not None:
            require_utc(item["valid_to"], f"{case_id}.experience.evidence[{index}].valid_to")
        if item["capture_mode"] not in {"hash_only", "snapshot", "pointer_only", "capture_failed"}:
            raise ContractError(f"{case_id}.experience.evidence[{index}].capture_mode invalid")
        if item["quality"] not in {"sufficient", "insufficient", "unknown"}:
            raise ContractError(f"{case_id}.experience.evidence[{index}].quality invalid")
        if item["commitment"] != evidence_commitment(case_id, item):
            raise ContractError(f"{case_id}.experience.evidence[{index}] commitment mismatch")
    lineage = experience["lineage"]
    if not isinstance(lineage, Mapping) or set(lineage) != {"derivation_status", "parent_experience_ids", "raw_source_status", "commitment"}:
        raise ContractError(f"{case_id}.experience.lineage shape mismatch")
    if lineage["derivation_status"] not in {"direct", "derived", "contaminated"}:
        raise ContractError(f"{case_id}.experience.lineage.derivation_status invalid")
    if lineage["raw_source_status"] not in {"available", "revoked", "deleted", "unknown"}:
        raise ContractError(f"{case_id}.experience.lineage.raw_source_status invalid")
    if not isinstance(lineage["parent_experience_ids"], list) or any(not isinstance(item, str) for item in lineage["parent_experience_ids"]):
        raise ContractError(f"{case_id}.experience.lineage.parent_experience_ids invalid")
    require_digest(lineage["commitment"], f"{case_id}.experience.lineage.commitment")
    if lineage["commitment"] != lineage_commitment(lineage, experience["experience_id"]):
        raise ContractError(f"{case_id}.experience.lineage commitment mismatch")


def validate_agent_view(case_id: str, view: Mapping[str, Any]) -> None:
    required = {"case_id", "task_b", "world", "experiences", "authority", "revalidation", "controls"}
    if set(view) != required:
        raise ContractError(f"{case_id}.agent_view shape mismatch")
    if view["case_id"] != case_id:
        raise ContractError(f"{case_id}.agent_view case binding mismatch")
    require_identifier(view["task_b"]["task_id"], f"{case_id}.agent_view.task_b.task_id")
    world = view["world"]
    if not isinstance(world, Mapping) or set(world) != {"world_state_id", "version", "status", "facts_code", "state_hash"}:
        raise ContractError(f"{case_id}.agent_view.world shape mismatch")
    require_identifier(world["world_state_id"], f"{case_id}.agent_view.world.world_state_id")
    require_nonnegative_int(world["version"], f"{case_id}.agent_view.world.version")
    if world["status"] not in {"current", "stale", "superseded"}:
        raise ContractError(f"{case_id}.agent_view.world.status invalid")
    require_identifier(world["facts_code"], f"{case_id}.agent_view.world.facts_code")
    require_digest(world["state_hash"], f"{case_id}.agent_view.world.state_hash")
    if world["state_hash"] != world_commitment(world):
        raise ContractError(f"{case_id}.agent_view.world commitment mismatch")
    experiences = view["experiences"]
    if not isinstance(experiences, list) or not experiences:
        raise ContractError(f"{case_id}.agent_view.experiences must be non-empty")
    memory_ids: set[str] = set()
    for index, exp in enumerate(experiences):
        if not isinstance(exp, Mapping):
            raise ContractError(f"{case_id}.agent_view.experiences[{index}] is not an object")
        required_exp = {"experience_id", "memory_id", "approach_type", "approach_outcome", "validity_status", "world_state_hash", "source", "evidence", "lineage", "scope"}
        if set(exp) != required_exp:
            raise ContractError(f"{case_id}.agent_view.experiences[{index}] shape mismatch")
        for field in ("experience_id", "memory_id", "approach_type", "approach_outcome", "validity_status", "scope"):
            require_identifier(exp[field], f"{case_id}.agent_view.experiences[{index}].{field}")
        if exp["memory_id"] in memory_ids:
            raise ContractError(f"{case_id}.agent_view duplicate memory id")
        memory_ids.add(exp["memory_id"])
        require_digest(exp["world_state_hash"], f"{case_id}.agent_view.experiences[{index}].world_state_hash")
        if not isinstance(exp["source"], Mapping) or not isinstance(exp["evidence"], list) or not isinstance(exp["lineage"], Mapping):
            raise ContractError(f"{case_id}.agent_view.experiences[{index}] provenance shape invalid")
        source = exp["source"]
        if set(source) != {"source_id", "commitment", "scope", "capture_mode", "captured_at"}:
            raise ContractError(f"{case_id}.agent_view source shape mismatch")
        if source["commitment"] != source_commitment(case_id, source):
            raise ContractError(f"{case_id}.agent_view source commitment mismatch")
        for item in exp["evidence"]:
            if not isinstance(item, Mapping) or item.get("commitment") != evidence_commitment(case_id, item):
                raise ContractError(f"{case_id}.agent_view evidence commitment mismatch")
        if exp["lineage"].get("commitment") != lineage_commitment(exp["lineage"], exp["experience_id"]):
            raise ContractError(f"{case_id}.agent_view lineage commitment mismatch")
    authority = view["authority"]
    if not isinstance(authority, Mapping) or set(authority) != {"authority_id", "captured_version", "current_version", "status", "permitted_actions", "effective_at", "commitment"}:
        raise ContractError(f"{case_id}.agent_view.authority shape mismatch")
    require_identifier(authority["authority_id"], f"{case_id}.agent_view.authority.authority_id")
    for field in ("captured_version", "current_version"):
        require_nonnegative_int(authority[field], f"{case_id}.agent_view.authority.{field}")
    if authority["status"] not in {"active", "rotated", "revoked"}:
        raise ContractError(f"{case_id}.agent_view.authority.status invalid")
    if not isinstance(authority["permitted_actions"], list) or any(not isinstance(item, str) for item in authority["permitted_actions"]):
        raise ContractError(f"{case_id}.agent_view.authority.permitted_actions invalid")
    require_utc(authority["effective_at"], f"{case_id}.agent_view.authority.effective_at")
    require_digest(authority["commitment"], f"{case_id}.agent_view.authority.commitment")
    if authority["commitment"] != authority_commitment(authority):
        raise ContractError(f"{case_id}.agent_view.authority commitment mismatch")
    revalidation = view["revalidation"]
    required_revalidation = {"required", "performed", "result", "current_world_state_hash", "checked_at", "commitment"}
    if not isinstance(revalidation, Mapping) or set(revalidation) != required_revalidation:
        raise ContractError(f"{case_id}.agent_view.revalidation shape mismatch")
    for field in ("required", "performed"):
        require_bool(revalidation[field], f"{case_id}.agent_view.revalidation.{field}")
    if revalidation["result"] not in {"pass", "fail", "not_run"}:
        raise ContractError(f"{case_id}.agent_view.revalidation.result invalid")
    require_digest(revalidation["current_world_state_hash"], f"{case_id}.agent_view.revalidation.current_world_state_hash")
    require_utc(revalidation["checked_at"], f"{case_id}.agent_view.revalidation.checked_at")
    require_digest(revalidation["commitment"], f"{case_id}.agent_view.revalidation.commitment")
    if revalidation["commitment"] != revalidation_commitment(revalidation, experiences[0]["experience_id"]):
        raise ContractError(f"{case_id}.agent_view.revalidation commitment mismatch")
    controls = view["controls"]
    if not isinstance(controls, Mapping) or set(controls) != {"workspace_scope"}:
        raise ContractError(f"{case_id}.agent_view.controls shape mismatch")
    require_identifier(controls["workspace_scope"], f"{case_id}.agent_view.controls.workspace_scope")
    # Expected labels, gold strings, and oracle fields are forbidden in the
    # model-facing view. This is a structural leakage gate, not a regex guess.
    reject_forbidden(view, path=f"$.cases[{case_id}].agent_view")


def validate_corpus(corpus: Mapping[str, Any]) -> None:
    required = {"schema", "seed", "pair_count", "category_counts", "cases", "public_boundary"}
    if set(corpus) != required:
        raise ContractError("corpus top-level shape mismatch")
    if corpus["schema"] != CORPUS_SCHEMA:
        raise ContractError("corpus schema mismatch")
    require_nonnegative_int(corpus["seed"], "corpus.seed")
    if corpus["pair_count"] != 24:
        raise ContractError("corpus must contain exactly 24 pairs")
    if corpus["category_counts"] != EXPECTED_CATEGORY_COUNTS:
        raise ContractError("corpus category balance mismatch")
    if not isinstance(corpus["public_boundary"], str) or "synthetic" not in corpus["public_boundary"]:
        raise ContractError("corpus public boundary missing")
    cases = corpus["cases"]
    if not isinstance(cases, list) or len(cases) != corpus["pair_count"]:
        raise ContractError("corpus case count mismatch")
    case_ids: set[str] = set()
    task_ids: set[str] = set()
    category_counts = {key: 0 for key in REQUIRED_CATEGORIES}
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ContractError(f"case {index} is not an object")
        required_case = {
            "case_id", "pair_id", "deterministic_seed", "category", "task_a", "task_b",
            "world_history", "experience", "alternatives", "authority", "revalidation",
            "transition_chain", "transition_attempts", "controls", "evaluation", "commitments", "agent_view",
        }
        if set(case) != required_case:
            raise ContractError(f"case {index} shape mismatch")
        case_id = require_identifier(case["case_id"], f"case[{index}].case_id")
        if case_id in case_ids:
            raise ContractError(f"duplicate case id {case_id}")
        case_ids.add(case_id)
        require_identifier(case["pair_id"], f"case[{index}].pair_id")
        require_nonnegative_int(case["deterministic_seed"], f"case[{index}].deterministic_seed")
        category = case["category"]
        if category not in REQUIRED_CATEGORIES:
            raise ContractError(f"case[{index}] unknown category")
        category_counts[category] += 1
        for actor in ("task_a", "task_b"):
            task = case[actor]
            if not isinstance(task, Mapping):
                raise ContractError(f"case[{index}].{actor} malformed")
            for field in ("task_id", "goal_code"):
                require_identifier(task.get(field), f"case[{index}].{actor}.{field}")
            if task["task_id"] in task_ids:
                raise ContractError(f"duplicate task id {task['task_id']}")
            task_ids.add(task["task_id"])
            require_utc(task["captured_at"], f"case[{index}].{actor}.captured_at")
        world_history = case["world_history"]
        if not isinstance(world_history, list) or len(world_history) != 2:
            raise ContractError(f"case[{index}].world_history must contain prior/current")
        for world in world_history:
            if not isinstance(world, Mapping) or set(world) != {"world_state_id", "version", "status", "facts_code", "state_hash"}:
                raise ContractError(f"case[{index}] world shape mismatch")
            if world["state_hash"] != world_commitment(world):
                raise ContractError(f"case[{index}] world hash mismatch")
        validate_experience(case_id, case["experience"])
        full_controls = case["controls"]
        if not isinstance(full_controls, Mapping) or set(full_controls) != {"negative_control", "control_type", "tamper_target", "cross_workspace_distractor", "leakage_partition"}:
            raise ContractError(f"case[{index}].controls shape mismatch")
        require_bool(full_controls["negative_control"], f"case[{index}].controls.negative_control")
        require_identifier(full_controls["control_type"], f"case[{index}].controls.control_type")
        require_identifier(full_controls["tamper_target"], f"case[{index}].controls.tamper_target")
        if full_controls["cross_workspace_distractor"] is not None:
            require_identifier(full_controls["cross_workspace_distractor"], f"case[{index}].controls.cross_workspace_distractor")
        require_identifier(full_controls["leakage_partition"], f"case[{index}].controls.leakage_partition")
        if full_controls["leakage_partition"] != case["pair_id"]:
            raise ContractError(f"case[{index}] leakage partition mismatch")
        if full_controls["negative_control"] and full_controls["control_type"] == "none":
            raise ContractError(f"case[{index}] negative control lacks control type")
        alternatives = case["alternatives"]
        if not isinstance(alternatives, list):
            raise ContractError(f"case[{index}].alternatives must be a list")
        for alternative in alternatives:
            validate_experience(case_id, alternative)
        evaluation = case["evaluation"]
        if not isinstance(evaluation, Mapping) or set(evaluation) != {"expected_decision_class", "expected_reason_code", "risk_case", "requires_revalidation", "expected_revalidated"}:
            raise ContractError(f"case[{index}].evaluation shape mismatch")
        expected = evaluation["expected_decision_class"]
        if expected not in ALLOWED_DECISIONS:
            raise ContractError(f"case[{index}] expected decision invalid")
        require_identifier(evaluation["expected_reason_code"], f"case[{index}].evaluation.expected_reason_code")
        require_bool(evaluation["risk_case"], f"case[{index}].evaluation.risk_case")
        require_bool(evaluation["requires_revalidation"], f"case[{index}].evaluation.requires_revalidation")
        require_bool(evaluation["expected_revalidated"], f"case[{index}].evaluation.expected_revalidated")
        validate_transition_chain(case["transition_chain"], expected, f"case[{index}].transition_chain")
        attempts = case["transition_attempts"]
        if not isinstance(attempts, list):
            raise ContractError(f"case[{index}].transition_attempts must be a list")
        for attempt in attempts:
            if not isinstance(attempt, Mapping) or set(attempt) != {"state", "event_id", "authorized", "expected_disposition"}:
                raise ContractError(f"case[{index}] transition attempt shape mismatch")
            require_identifier(attempt["state"], f"case[{index}] transition attempt state")
            require_identifier(attempt["event_id"], f"case[{index}] transition attempt event_id")
            require_bool(attempt["authorized"], f"case[{index}] transition attempt authorized")
            require_identifier(attempt["expected_disposition"], f"case[{index}] transition attempt disposition")
            if attempt["authorized"] and attempt["expected_disposition"] != "accepted":
                raise ContractError(f"case[{index}] authorized transition is not accepted")
            if not attempt["authorized"] and attempt["expected_disposition"] != "rejected":
                raise ContractError(f"case[{index}] unauthorized transition is not rejected")
        commitments = case["commitments"]
        if not isinstance(commitments, Mapping) or set(commitments) != {"world_history_sha256", "agent_view_sha256", "label_sha256", "case_sha256"}:
            raise ContractError(f"case[{index}] commitments shape mismatch")
        for key, value in commitments.items():
            require_digest(value, f"case[{index}].commitments.{key}")
        if commitments["world_history_sha256"] != canonical_digest(world_history):
            raise ContractError(f"case[{index}] world history commitment mismatch")
        validate_agent_view(case_id, case["agent_view"])
        if commitments["agent_view_sha256"] != canonical_digest(case["agent_view"]):
            raise ContractError(f"case[{index}] agent view commitment mismatch")
        if commitments["case_sha256"] != canonical_digest({
            "case_id": case_id,
            "pair_id": case["pair_id"],
            "deterministic_seed": case["deterministic_seed"],
            "category": category,
            "world_history_sha256": commitments["world_history_sha256"],
            "agent_view_sha256": commitments["agent_view_sha256"],
            "label_sha256": commitments["label_sha256"],
        }):
            raise ContractError(f"case[{index}] case commitment mismatch")
    if category_counts != EXPECTED_CATEGORY_COUNTS:
        raise ContractError(f"corpus categories observed {category_counts}")


def validate_shared_views(bundle: Mapping[str, Any]) -> None:
    required = {"schema", "seed", "pair_count", "public_boundary", "cases"}
    if set(bundle) != required or bundle["schema"] != SHARED_VIEWS_SCHEMA:
        raise ContractError("shared agent-view bundle shape/schema mismatch")
    require_nonnegative_int(bundle["seed"], "shared.seed")
    if bundle["pair_count"] != 24 or not isinstance(bundle["cases"], list) or len(bundle["cases"]) != 24:
        raise ContractError("shared agent-view pair count mismatch")
    if not isinstance(bundle["public_boundary"], str) or "label" not in bundle["public_boundary"]:
        raise ContractError("shared agent-view boundary missing")
    seen: set[str] = set()
    for index, row in enumerate(bundle["cases"]):
        if not isinstance(row, Mapping) or set(row) != {"case_id", "pair_id", "deterministic_seed", "agent_view", "agent_view_sha256"}:
            raise ContractError(f"shared case {index} shape mismatch")
        case_id = require_identifier(row["case_id"], f"shared.case[{index}].case_id")
        if case_id in seen:
            raise ContractError(f"shared duplicate case id {case_id}")
        seen.add(case_id)
        require_identifier(row["pair_id"], f"shared.case[{index}].pair_id")
        require_nonnegative_int(row["deterministic_seed"], f"shared.case[{index}].deterministic_seed")
        validate_agent_view(case_id, row["agent_view"])
        require_digest(row["agent_view_sha256"], f"shared.case[{index}].agent_view_sha256")
        if row["agent_view_sha256"] != canonical_digest(row["agent_view"]):
            raise ContractError(f"shared case {index} view commitment mismatch")
        text = json.dumps(row, sort_keys=True).lower()
        for marker in ("expected_decision_class", "expected_reason_code", "negative_control", "control_type", "fresh-runbook", "revoked-source"):
            if marker in text:
                raise ContractError(f"shared case {index} leaks evaluator marker {marker}")
    reject_forbidden(bundle)


def validate_adapter_result(result: Mapping[str, Any], *, case_id: str, adapter: str) -> None:
    required = {
        "adapter", "case_id", "decision", "reason_code", "revalidated", "provenance_validated",
        "authority_checked", "unsafe_reuse", "selected_memory_count", "transition_steps",
    }
    if set(result) != required:
        raise ContractError(f"{adapter}/{case_id} result shape mismatch")
    if result["adapter"] != adapter or result["case_id"] != case_id:
        raise ContractError(f"{adapter}/{case_id} result identity mismatch")
    if result["decision"] not in ALLOWED_DECISIONS:
        raise ContractError(f"{adapter}/{case_id} decision invalid")
    require_identifier(result["reason_code"], f"{adapter}/{case_id}.reason_code")
    for key in ("revalidated", "provenance_validated", "authority_checked", "unsafe_reuse"):
        require_bool(result[key], f"{adapter}/{case_id}.{key}")
    require_nonnegative_int(result["selected_memory_count"], f"{adapter}/{case_id}.selected_memory_count")
    require_nonnegative_int(result["transition_steps"], f"{adapter}/{case_id}.transition_steps")


def validate_public_report(report: Mapping[str, Any]) -> None:
    required = {"schema", "status", "provider_calls", "judge_calls", "corpus_sha256", "manifest_sha256", "adapters", "metrics", "case_results", "report_signature_sha256", "claim_boundary"}
    if set(report) != required:
        raise ContractError("public report top-level shape mismatch")
    if report["schema"] != REPORT_SCHEMA or report["status"] != "provider_free_contract_validation":
        raise ContractError("public report schema/status mismatch")
    require_nonnegative_int(report["provider_calls"], "report.provider_calls")
    require_nonnegative_int(report["judge_calls"], "report.judge_calls")
    if report["provider_calls"] != 0 or report["judge_calls"] != 0:
        raise ContractError("provider-free report contains provider calls")
    require_digest(report["corpus_sha256"], "report.corpus_sha256")
    require_digest(report["manifest_sha256"], "report.manifest_sha256")
    if not isinstance(report["adapters"], list) or not report["adapters"]:
        raise ContractError("report adapters missing")
    for adapter in report["adapters"]:
        if not isinstance(adapter, Mapping) or set(adapter) != {"name", "version", "status", "reason", "provider_calls", "judge_calls"}:
            raise ContractError("report adapter row shape mismatch")
        require_identifier(adapter["name"], "report.adapter.name")
        require_identifier(adapter["version"], "report.adapter.version")
        if adapter["status"] not in ALLOWED_STATUSES:
            raise ContractError("report adapter status invalid")
        require_nonnegative_int(adapter["provider_calls"], "report.adapter.provider_calls")
        require_nonnegative_int(adapter["judge_calls"], "report.adapter.judge_calls")
        if not isinstance(adapter["reason"], str) or not adapter["reason"]:
            raise ContractError("report adapter reason missing")
    metrics = report["metrics"]
    if not isinstance(metrics, list) or not metrics:
        raise ContractError("report metrics missing")
    for metric in metrics:
        if not isinstance(metric, Mapping) or set(metric) != ALLOWED_PUBLIC_METRIC_KEYS:
            raise ContractError("report metric shape mismatch")
        require_identifier(metric["metric"], "report.metric.metric")
        if metric["status"] not in ALLOWED_STATUSES:
            raise ContractError("report metric status invalid")
        require_nonnegative_int(metric["numerator"], "report.metric.numerator")
        require_nonnegative_int(metric["denominator"], "report.metric.denominator")
        if metric["denominator"] == 0 and metric["status"] == "pass":
            raise ContractError("passing metric has zero denominator")
        if metric["numerator"] > metric["denominator"]:
            raise ContractError("metric numerator exceeds denominator")
        require_rate(metric["rate"], "report.metric.rate")
        if metric["denominator"] and not math.isclose(metric["rate"], metric["numerator"] / metric["denominator"], abs_tol=0.00005):
            raise ContractError("metric rate/counter mismatch")
        if metric["polarity"] not in {"higher_is_better", "lower_is_better", "coverage"}:
            raise ContractError("metric polarity invalid")
        if not isinstance(metric["scope"], str) or not metric["scope"]:
            raise ContractError("metric scope missing")
    case_results = report["case_results"]
    if not isinstance(case_results, list) or len(case_results) != 24 * 3:
        raise ContractError("public case result denominator mismatch")
    seen: set[tuple[str, str]] = set()
    for row in case_results:
        if not isinstance(row, Mapping) or set(row) != ALLOWED_PUBLIC_CASE_KEYS:
            raise ContractError("public case result shape mismatch")
        require_identifier(row["case_id"], "public case.case_id")
        require_identifier(row["adapter"], "public case.adapter")
        if (row["case_id"], row["adapter"]) in seen:
            raise ContractError("duplicate public case result")
        seen.add((row["case_id"], row["adapter"]))
        if row["decision"] not in ALLOWED_DECISIONS:
            raise ContractError("public case decision invalid")
        require_identifier(row["reason_code"], "public case.reason_code")
        for key in ("decision_match", "negative_control", "revalidated", "provenance_validated", "authority_checked", "unsafe_reuse"):
            require_bool(row[key], f"public case.{key}")
        for key in ("selected_memory_count", "transition_steps"):
            require_nonnegative_int(row[key], f"public case.{key}")
    boundary = report["claim_boundary"]
    if not isinstance(boundary, Mapping) or set(boundary) != {"supported", "not_supported", "unexecuted"}:
        raise ContractError("report claim boundary shape mismatch")
    for key in ("supported", "not_supported", "unexecuted"):
        if not isinstance(boundary[key], list) or any(not isinstance(item, str) for item in boundary[key]):
            raise ContractError(f"report claim boundary {key} malformed")
    reject_forbidden(report)


def public_report_signature(report: Mapping[str, Any]) -> str:
    body = dict(report)
    body.pop("report_signature_sha256", None)
    return canonical_digest(body)
