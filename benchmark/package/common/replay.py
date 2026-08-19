"""Versioned, hash-only retrieval replay envelopes.

This module is the provider-free boundary shared by benchmark lanes.  It
records retrieval membership and ordering without publishing query text,
memory bodies, credentials, or gold labels.  A replay can therefore validate a
synthetic snapshot's candidate membership/order and telemetry semantics, but it
cannot establish downstream answer quality.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "perseus-vault-retrieval-replay/v1"
SNAPSHOT_SCHEMA_VERSION = "perseus-vault-retrieval-snapshot/v1"
_SEQUENCE_POLICIES = {"wire_v1", "chronological_sequence_v1", "identity_v1"}
_STATUSES = {"complete", "degraded", "partial", "empty", "unavailable"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_FORBIDDEN_MARKERS = ("password", "secret", "credential", "access_token", "api_key", "authorization")


class ReplayValidationError(ValueError):
    """Raised when a replay envelope or snapshot violates the contract."""


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReplayValidationError("value is not canonical JSON") from exc


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ReplayValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ReplayValidationError(f"{field} must be a bounded public identifier")
    lowered = value.lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        raise ReplayValidationError(f"{field} contains a forbidden private marker")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReplayValidationError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayValidationError(f"{field} must be a non-negative integer")
    return value


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ReplayValidationError(f"{field} must be finite")
    return float(value)


def _hash_identifier(value: str) -> str:
    return sha256_text(value)


def _raw_candidate(candidate: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise ReplayValidationError(f"candidate {index} must be an object")
    required = {"candidate_id", "source_ref", "content", "provenance", "wire_rank", "original_position"}
    missing = sorted(required - set(candidate))
    if missing:
        raise ReplayValidationError(f"candidate {index} is missing {missing[0]}")
    candidate_id = _id(candidate["candidate_id"], f"candidate {index}.candidate_id")
    source_ref = _id(candidate["source_ref"], f"candidate {index}.source_ref")
    provenance = _id(candidate["provenance"], f"candidate {index}.provenance")
    content = candidate["content"]
    if not isinstance(content, str):
        raise ReplayValidationError(f"candidate {index}.content must be text")
    wire_rank = _positive_int(candidate["wire_rank"], f"candidate {index}.wire_rank")
    original_position = _positive_int(candidate["original_position"], f"candidate {index}.original_position")
    score_present = "score" in candidate
    score_semantics_present = "score_semantics" in candidate
    if score_present != score_semantics_present:
        raise ReplayValidationError("score and score_semantics must be provided together")
    raw: dict[str, Any] = {
        "candidate_id": candidate_id,
        "source_ref": source_ref,
        "content": content,
        "provenance": provenance,
        "wire_rank": wire_rank,
        "original_position": original_position,
    }
    if score_present:
        raw["score"] = _finite_number(candidate["score"], f"candidate {index}.score")
        raw["score_semantics"] = _id(candidate["score_semantics"], f"candidate {index}.score_semantics")
    unknown = set(candidate) - set(raw)
    if unknown:
        raise ReplayValidationError(f"candidate contains unknown field: {sorted(unknown)[0]}")
    return raw


def _public_candidate(raw: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidate_id_sha256": _hash_identifier(raw["candidate_id"]),
        "source_ref_sha256": _hash_identifier(raw["source_ref"]),
        "content_sha256": sha256_text(raw["content"]),
        "content_chars": len(raw["content"]),
        "provenance_sha256": _hash_identifier(raw["provenance"]),
        "wire_rank": raw["wire_rank"],
        "original_position": raw["original_position"],
    }
    if "score" in raw:
        result["score"] = raw["score"]
        result["score_semantics"] = raw["score_semantics"]
    return result


def _validate_public_candidate(candidate: Any, index: int) -> None:
    if not isinstance(candidate, dict):
        raise ReplayValidationError(f"public candidate {index} must be an object")
    allowed = {
        "candidate_id_sha256", "source_ref_sha256", "content_sha256", "content_chars", "provenance_sha256",
        "wire_rank", "original_position", "final_rank", "score", "score_semantics",
    }
    unknown = set(candidate) - allowed
    if unknown:
        raise ReplayValidationError(f"public candidate contains unknown field: {sorted(unknown)[0]}")
    for field in ("candidate_id_sha256", "source_ref_sha256", "content_sha256", "provenance_sha256"):
        _sha(candidate.get(field), f"candidate {index}.{field}")
    _nonnegative_int(candidate.get("content_chars"), f"candidate {index}.content_chars")
    for field in ("wire_rank", "original_position", "final_rank"):
        if field in candidate:
            _positive_int(candidate[field], f"candidate {index}.{field}")
    if ("score" in candidate) != ("score_semantics" in candidate):
        raise ReplayValidationError("score and score_semantics must remain paired")
    if "score" in candidate:
        _finite_number(candidate["score"], f"candidate {index}.score")
        _id(candidate["score_semantics"], f"candidate {index}.score_semantics")


def build_snapshot(candidates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a hash-only replay snapshot from internal candidate rows."""
    raw = [_raw_candidate(candidate, index) for index, candidate in enumerate(candidates)]
    wire_ranks = [row["wire_rank"] for row in raw]
    if sorted(wire_ranks) != list(range(1, len(raw) + 1)):
        raise ReplayValidationError("wire ranks must be contiguous from one")
    positions = [row["original_position"] for row in raw]
    if len(set(positions)) != len(positions):
        raise ReplayValidationError("original positions must be unique")
    records = [_public_candidate(row) for row in raw]
    records.sort(key=lambda row: (row["candidate_id_sha256"], row["wire_rank"]))
    base = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "records": records,
        "raw_inputs_captured": False,
    }
    snapshot = {**base, "snapshot_sha256": sha256_text(stable_json(base))}
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: Any) -> None:
    if not isinstance(snapshot, dict):
        raise ReplayValidationError("snapshot must be an object")
    allowed = {"schema_version", "records", "raw_inputs_captured", "snapshot_sha256"}
    unknown = set(snapshot) - allowed
    if unknown:
        raise ReplayValidationError(f"snapshot contains unknown field: {sorted(unknown)[0]}")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ReplayValidationError("unsupported snapshot schema")
    if snapshot.get("raw_inputs_captured") is not False:
        raise ReplayValidationError("snapshot must declare raw_inputs_captured=false")
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ReplayValidationError("snapshot records must be a list")
    for index, record in enumerate(records):
        _validate_public_candidate(record, index)
        if "final_rank" in record:
            raise ReplayValidationError("snapshot records cannot contain final_rank")
    if len({record["candidate_id_sha256"] for record in records}) != len(records):
        raise ReplayValidationError("snapshot candidate identifiers must be unique")
    base = {key: snapshot[key] for key in ("schema_version", "records", "raw_inputs_captured")}
    if snapshot.get("snapshot_sha256") != sha256_text(stable_json(base)):
        raise ReplayValidationError("snapshot digest mismatch")


def _sequence_order(raw: list[dict[str, Any]], policy: str) -> list[dict[str, Any]]:
    if policy == "wire_v1":
        return sorted(raw, key=lambda row: row["wire_rank"])
    if policy == "chronological_sequence_v1":
        return sorted(raw, key=lambda row: (row["original_position"], row["candidate_id"]))
    if policy == "identity_v1":
        return sorted(raw, key=lambda row: row["candidate_id"])
    raise ReplayValidationError(f"unsupported sequence policy: {policy}")


def _context_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayValidationError(f"{field} must be non-empty")
    return sha256_text(value)


def build_envelope(
    *,
    workspace_id: str,
    scope: str,
    fixture_id: str,
    corpus_sha256: str,
    retrieval_profile: str,
    mode: str,
    top_k: int,
    cell_id: str,
    request_sha256: str,
    config_sha256: str,
    code_sha256: str,
    context_policy: str,
    context_policy_version: str,
    snapshot: dict[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    sequence_policy: str = "wire_v1",
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic public envelope from internal retrieval rows."""
    validate_snapshot(snapshot)
    _id(fixture_id, "fixture_id")
    _id(retrieval_profile, "retrieval_profile")
    _id(mode, "mode")
    _id(cell_id, "cell_id")
    _id(context_policy, "context_policy")
    _id(context_policy_version, "context_policy_version")
    if sequence_policy not in _SEQUENCE_POLICIES:
        raise ReplayValidationError(f"unsupported sequence policy: {sequence_policy}")
    top_k = _positive_int(top_k, "top_k")
    for field, value in (("corpus_sha256", corpus_sha256), ("request_sha256", request_sha256),
                         ("config_sha256", config_sha256), ("code_sha256", code_sha256)):
        _sha(value, field)
    raw = [_raw_candidate(candidate, index) for index, candidate in enumerate(candidates)]
    wire_ranks = [row["wire_rank"] for row in raw]
    if sorted(wire_ranks) != list(range(1, len(raw) + 1)):
        raise ReplayValidationError("wire ranks must be contiguous from one")
    if len({row["original_position"] for row in raw}) != len(raw):
        raise ReplayValidationError("original positions must be unique")
    expected_snapshot = build_snapshot(raw)
    if expected_snapshot != snapshot:
        raise ReplayValidationError("snapshot does not match retrieval candidates")
    ordered = _sequence_order(raw, sequence_policy)
    delivered = ordered[:top_k]
    candidate_count = len(raw)
    if status is None:
        if not raw:
            status = "empty"
        elif len(raw) >= top_k:
            status = "complete"
        else:
            status = "partial"
            reason = reason or "top_k_incomplete"
    if status not in _STATUSES:
        raise ReplayValidationError("invalid retrieval status")
    if status == "empty" and raw:
        raise ReplayValidationError("empty status requires no candidates")
    if status == "unavailable" and raw:
        raise ReplayValidationError("unavailable status requires no candidates")
    if status == "complete" and (len(raw) < top_k or len(delivered) != top_k):
        raise ReplayValidationError("complete status requires a complete requested top-k")
    if status in {"degraded", "partial", "unavailable"} and not reason:
        raise ReplayValidationError(f"{status} status requires a reason")
    if reason is not None:
        _id(reason, "reason")
    public_candidates: list[dict[str, Any]] = []
    for final_rank, row in enumerate(delivered, 1):
        public = _public_candidate(row)
        public["final_rank"] = final_rank
        public_candidates.append(public)
    membership = {
        "candidate_count": candidate_count,
        "delivered_count": len(public_candidates),
        "requested_top_k": top_k,
        "complete": candidate_count >= top_k and len(public_candidates) == top_k,
        "truncated": candidate_count > len(public_candidates),
    }
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "workspace_sha256": _context_digest(workspace_id, "workspace_id"),
        "scope_sha256": _context_digest(scope, "scope"),
        "fixture_id": fixture_id,
        "corpus_sha256": corpus_sha256,
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "retrieval": {
            "profile": retrieval_profile,
            "mode": mode,
            "top_k": top_k,
            "sequence_policy": sequence_policy,
        },
        "request": {"cell_id": cell_id, "request_sha256": request_sha256},
        "commitments": {"config_sha256": config_sha256, "code_sha256": code_sha256},
        "context_policy": {"name": context_policy, "version": context_policy_version},
        "status": status,
        "membership": membership,
        "candidates": public_candidates,
        "raw_inputs_captured": False,
        "network_calls": 0,
    }
    if reason is not None:
        base["reason"] = reason
    replay_fingerprint = _replay_fingerprint(base)
    with_replay = {**base, "replay_fingerprint_sha256": replay_fingerprint}
    envelope = {**with_replay, "projection_sha256": sha256_text(stable_json(with_replay))}
    validate_envelope(envelope)
    return envelope


def _replay_fingerprint(envelope_without_hashes: Mapping[str, Any]) -> str:
    material = {
        "status": envelope_without_hashes["status"],
        "membership": envelope_without_hashes["membership"],
        "candidates": envelope_without_hashes["candidates"],
        "retrieval": envelope_without_hashes["retrieval"],
        "snapshot_sha256": envelope_without_hashes["snapshot_sha256"],
    }
    return sha256_text(stable_json(material))


def validate_envelope(envelope: Any) -> None:
    if not isinstance(envelope, dict):
        raise ReplayValidationError("envelope must be an object")
    allowed = {
        "schema_version", "workspace_sha256", "scope_sha256", "fixture_id", "corpus_sha256",
        "snapshot_sha256", "retrieval", "request", "commitments", "context_policy", "status",
        "reason", "membership", "candidates", "raw_inputs_captured", "network_calls",
        "replay_fingerprint_sha256", "projection_sha256",
    }
    unknown = set(envelope) - allowed
    if unknown:
        raise ReplayValidationError(f"envelope contains unknown field: {sorted(unknown)[0]}")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise ReplayValidationError("unsupported replay schema")
    for field in ("workspace_sha256", "scope_sha256", "corpus_sha256", "snapshot_sha256"):
        _sha(envelope.get(field), field)
    _id(envelope.get("fixture_id"), "fixture_id")
    _sha(envelope.get("replay_fingerprint_sha256"), "replay_fingerprint_sha256")
    _sha(envelope.get("projection_sha256"), "projection_sha256")
    retrieval = envelope.get("retrieval")
    if not isinstance(retrieval, dict) or set(retrieval) != {"profile", "mode", "top_k", "sequence_policy"}:
        raise ReplayValidationError("retrieval commitment is malformed")
    _id(retrieval["profile"], "retrieval.profile")
    _id(retrieval["mode"], "retrieval.mode")
    top_k = _positive_int(retrieval["top_k"], "retrieval.top_k")
    if retrieval["sequence_policy"] not in _SEQUENCE_POLICIES:
        raise ReplayValidationError("retrieval.sequence_policy is invalid")
    request = envelope.get("request")
    if not isinstance(request, dict) or set(request) != {"cell_id", "request_sha256"}:
        raise ReplayValidationError("request commitment is malformed")
    _id(request["cell_id"], "request.cell_id")
    _sha(request["request_sha256"], "request.request_sha256")
    commitments = envelope.get("commitments")
    if not isinstance(commitments, dict) or set(commitments) != {"config_sha256", "code_sha256"}:
        raise ReplayValidationError("code/config commitments are malformed")
    _sha(commitments["config_sha256"], "commitments.config_sha256")
    _sha(commitments["code_sha256"], "commitments.code_sha256")
    policy = envelope.get("context_policy")
    if not isinstance(policy, dict) or set(policy) != {"name", "version"}:
        raise ReplayValidationError("context policy is malformed")
    _id(policy["name"], "context_policy.name")
    _id(policy["version"], "context_policy.version")
    status = envelope.get("status")
    if status not in _STATUSES:
        raise ReplayValidationError("status is invalid")
    if envelope.get("raw_inputs_captured") is not False:
        raise ReplayValidationError("raw_inputs_captured must be false")
    if envelope.get("network_calls") != 0:
        raise ReplayValidationError("replay envelope must be provider-free")
    if status in {"degraded", "partial", "unavailable"}:
        _id(envelope.get("reason"), "reason")
    elif "reason" in envelope:
        raise ReplayValidationError("reason is only valid for degraded/partial/unavailable states")
    membership = envelope.get("membership")
    if not isinstance(membership, dict) or set(membership) != {"candidate_count", "delivered_count", "requested_top_k", "complete", "truncated"}:
        raise ReplayValidationError("membership is malformed")
    candidate_count = _nonnegative_int(membership["candidate_count"], "membership.candidate_count")
    delivered_count = _nonnegative_int(membership["delivered_count"], "membership.delivered_count")
    if membership["requested_top_k"] != top_k:
        raise ReplayValidationError("membership top_k mismatch")
    if not isinstance(membership["complete"], bool) or not isinstance(membership["truncated"], bool):
        raise ReplayValidationError("membership booleans are invalid")
    candidates = envelope.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != delivered_count or delivered_count > top_k:
        raise ReplayValidationError("delivered candidates are invalid")
    wire_ranks: list[int] = []
    final_ranks: list[int] = []
    positions: list[int] = []
    identifiers: list[str] = []
    for index, candidate in enumerate(candidates):
        _validate_public_candidate(candidate, index)
        if candidate.get("final_rank") != index + 1:
            raise ReplayValidationError("final ranks must be contiguous")
        wire_ranks.append(candidate["wire_rank"])
        final_ranks.append(candidate["final_rank"])
        positions.append(candidate["original_position"])
        identifiers.append(candidate["candidate_id_sha256"])
    if len(set(identifiers)) != len(identifiers):
        raise ReplayValidationError("delivered candidate identifiers must be unique")
    if len(set(wire_ranks)) != len(wire_ranks):
        raise ReplayValidationError("delivered wire ranks must be unique")
    if len(set(positions)) != len(positions):
        raise ReplayValidationError("delivered original positions must be unique")
    expected_complete = candidate_count >= top_k and delivered_count == top_k
    if membership["complete"] != expected_complete:
        raise ReplayValidationError("membership completeness is inconsistent")
    if membership["truncated"] != (candidate_count > delivered_count):
        raise ReplayValidationError("membership truncation is inconsistent")
    if status == "empty" and (candidate_count or delivered_count):
        raise ReplayValidationError("empty envelope contains candidates")
    if status == "unavailable" and (candidate_count or delivered_count):
        raise ReplayValidationError("unavailable envelope contains candidates")
    if status == "complete" and not membership["complete"]:
        raise ReplayValidationError("complete envelope has incomplete membership")
    replay_base = {key: value for key, value in envelope.items() if key not in {"replay_fingerprint_sha256", "projection_sha256"}}
    if envelope["replay_fingerprint_sha256"] != _replay_fingerprint(replay_base):
        raise ReplayValidationError("replay fingerprint mismatch")
    with_replay = {**replay_base, "replay_fingerprint_sha256": envelope["replay_fingerprint_sha256"]}
    if envelope["projection_sha256"] != sha256_text(stable_json(with_replay)):
        raise ReplayValidationError("projection digest mismatch")


def replay_envelope(envelope: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate membership/order against a hash-only snapshot."""
    validate_envelope(dict(envelope))
    validate_snapshot(dict(snapshot))
    if envelope["snapshot_sha256"] != snapshot["snapshot_sha256"]:
        raise ReplayValidationError("envelope snapshot commitment mismatch")
    by_id = {record["candidate_id_sha256"]: record for record in snapshot["records"]}
    for index, candidate in enumerate(envelope["candidates"]):
        record = by_id.get(candidate["candidate_id_sha256"])
        if record is None:
            raise ReplayValidationError(f"candidate {index} is absent from snapshot")
        for field in ("candidate_id_sha256", "source_ref_sha256", "content_sha256", "content_chars", "provenance_sha256", "wire_rank", "original_position"):
            if record.get(field) != candidate.get(field):
                raise ReplayValidationError(f"snapshot mismatch for {field}")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": envelope["status"],
        "candidate_count": len(envelope["candidates"]),
        "candidate_ids_sha256": [candidate["candidate_id_sha256"] for candidate in envelope["candidates"]],
        "final_ranks": [candidate["final_rank"] for candidate in envelope["candidates"]],
        "replay_fingerprint_sha256": envelope["replay_fingerprint_sha256"],
    }


__all__ = [
    "ReplayValidationError", "SCHEMA_VERSION", "SNAPSHOT_SCHEMA_VERSION", "build_envelope",
    "build_snapshot", "replay_envelope", "sha256_text", "stable_json", "validate_envelope",
    "validate_snapshot",
]
