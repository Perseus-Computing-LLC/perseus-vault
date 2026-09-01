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
import os
import re
import sqlite3
import subprocess
from pathlib import Path
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


RECALL_WIRE_SCHEMA_VERSION = "perseus-vault-recall-wire/v1"
_RECALL_WIRE_FIELDS = {"items", "total", "retrieval_profile", "variants", "diagnostic", "outcome", "gap", "gap_fill", "fused_trace", "conflict_flags", "abstain_hint", "conflict_flags_markdown", "freshness_summary", "freshness_gate", "evidence", "declared_graph"}
_RECALL_WIRE_STATUS = {"complete", "partial", "degraded", "empty", "unavailable"}
_RECALL_OUTCOME_STATUS = {"fresh", "complete", "partial", "degraded", "empty", "unavailable", "timeout", "stale"}
_RECALL_OBJECT_FIELDS = {"diagnostic", "outcome", "fused_trace", "freshness_summary", "freshness_gate", "evidence", "declared_graph"}
_RECALL_STRING_FIELDS = {"gap_fill", "conflict_flags_markdown"}
_RECALL_BOOL_FIELDS = {"gap", "abstain_hint"}
_RECALL_LIST_FIELDS = {"conflict_flags"}
_RECALL_ITEM_FIELDS = frozenset({
    "key", "id", "body_json", "score", "score_semantics", "decay_score", "why_served", "wire_rank",
})
_WHY_SERVED_FIELDS = frozenset({"reason", "memory_class"})
_PREFLIGHT_FIELDS = frozenset({
    "binary_sha256", "binary_commit", "binary_commit_sha256", "database_fresh",
    "database_identity", "database_id_sha256", "response_schema", "response_schema_sha256",
    "dataset_sha256", "config_sha256",
})
_PREFLIGHT_BINDING_FIELDS = ("binary_sha256", "binary_commit", "binary_commit_sha256",
                             "response_schema", "response_schema_sha256")
_RUNTIME_BINDING_FIELDS = frozenset({"binary", "db_path", "repo_root", "dataset", "config"})
_UNSET = object()


def _recall_wire_failure(reason: str = "malformed_recall_response") -> dict[str, Any]:
    return {
        "schema_version": RECALL_WIRE_SCHEMA_VERSION,
        "status": "unavailable",
        "reason": reason,
        "items": [],
        "total": 0,
    }


def _validate_recall_wire_projections(response: Mapping[str, Any]) -> None:
    if "variants" in response:
        _nonnegative_int(response["variants"], "variants")
    for field in _RECALL_OBJECT_FIELDS:
        if field in response and not isinstance(response[field], Mapping):
            raise ReplayValidationError(f"{field} must be an object when present")
    for field in _RECALL_STRING_FIELDS:
        if field in response and not isinstance(response[field], str):
            raise ReplayValidationError(f"{field} must be a string when present")
    for field in _RECALL_BOOL_FIELDS:
        if field in response and not isinstance(response[field], bool):
            raise ReplayValidationError(f"{field} must be a boolean when present")
    if "conflict_flags" in response and not isinstance(response["conflict_flags"], list):
        raise ReplayValidationError("conflict_flags must be a list when present")
    if "outcome" in response:
        outcome = response["outcome"]
        status = outcome.get("status")
        if not isinstance(status, str) or status.lower() not in _RECALL_OUTCOME_STATUS:
            raise ReplayValidationError("outcome.status is invalid")
    profile = response.get("retrieval_profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ReplayValidationError("retrieval_profile must be a non-empty string")


def _recall_wire_status(response: Mapping[str, Any], item_count: int, total: int, limit: int, offset: int) -> tuple[str, str | None]:
    if item_count == 0 and total > 0:
        return "unavailable", "empty_items_positive_total"
    if "outcome" in response:
        declared = response["outcome"]["status"].lower()
        if declared in {"timeout", "stale", "unavailable"}:
            return "unavailable", f"server_outcome_{declared}"
        if declared == "degraded":
            return "degraded", "server_recall_outcome"
        if declared == "partial":
            return "partial", "server_recall_outcome"
        if declared == "empty":
            if item_count:
                raise ReplayValidationError("empty outcome cannot contain items")
            if total:
                return "unavailable", "empty_outcome_positive_total"
            return "empty", None
        # `fresh` and wire-level `complete` use the envelope cardinality below.
    if item_count == 0 and total > 0:
        return "unavailable", "empty_items_positive_total"
    if item_count == 0:
        return "empty", None
    expected = min(limit, max(0, total - offset))
    if item_count < expected:
        return "partial", "short_recall_response"
    return "complete", None


def recall_status_is_scoreable(status: Any) -> bool:
    """Return whether a wire outcome may enter benchmark score denominators."""
    return isinstance(status, str) and status in {"complete", "empty"}


def _recall_item_key(item: Mapping[str, Any], index: int) -> str:
    if "key" in item:
        key = item["key"]
    elif "id" in item:
        key = item["id"]
    else:
        raise ReplayValidationError(f"recall item {index} lacks a key")
    if not isinstance(key, str) or not key:
        raise ReplayValidationError(f"recall item {index} has an invalid key")
    return key


def _validate_recall_item_projection(item: Mapping[str, Any], index: int) -> None:
    unknown = set(item) - _RECALL_ITEM_FIELDS
    if unknown:
        raise ReplayValidationError(f"recall item contains unknown field: {sorted(unknown)[0]}")
    if "why_served" in item:
        projection = item["why_served"]
        if not isinstance(projection, Mapping) or set(projection) - _WHY_SERVED_FIELDS:
            raise ReplayValidationError(f"recall item {index} has an unknown why_served projection")
        for field, value in projection.items():
            if not isinstance(value, str) or not value or len(value) > 256:
                raise ReplayValidationError(f"recall item {index}.why_served.{field} is malformed")
    if "wire_rank" in item:
        _positive_int(item["wire_rank"], f"recall item {index}.wire_rank")


def _recall_item_body(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    if "body_json" not in item:
        raise ReplayValidationError(f"recall item {index} lacks body_json")
    body = item["body_json"]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise ReplayValidationError(f"recall item {index} has malformed body_json") from exc
    if not isinstance(body, Mapping):
        raise ReplayValidationError(f"recall item {index} body_json must be an object")
    return dict(body)


def normalize_recall_response(response: Any, *, limit: int, offset: int = 0) -> dict[str, Any]:
    """Validate a live recall response without inventing ranking evidence.

    The returned item order is the server wire order.  ``wire_rank`` is added
    as a one-based, authoritative position.  ``score`` is copied only when the
    response explicitly provides a finite semantic score; ``decay_score`` is
    retained as a lifecycle/freshness signal and is never promoted to score.
    Malformed or RPC-error responses become a bounded ``unavailable`` result so
    callers cannot accidentally score an empty fallback as a successful miss.
    """
    try:
        limit = _positive_int(limit, "limit")
        offset = _nonnegative_int(offset, "offset")
        if not isinstance(response, Mapping):
            raise ReplayValidationError("recall response must be an object")
        if "error" in response or "items" not in response or "total" not in response:
            raise ReplayValidationError("recall response is missing its wire envelope")
        unknown = set(response) - _RECALL_WIRE_FIELDS
        if unknown:
            raise ReplayValidationError("recall response contains an unknown field")
        _validate_recall_wire_projections(response)
        items = response["items"]
        total = response["total"]
        if not isinstance(items, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ReplayValidationError("recall response envelope has invalid types")
        if not items and total > 0:
            raise ReplayValidationError("empty items with positive total")
        if total < len(items):
            raise ReplayValidationError("recall response total is below item count")
        profile = response.get("retrieval_profile")
        if items and profile is None and "variants" not in response:
            raise ReplayValidationError("non-empty recall response lacks retrieval_profile")
        normalized: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise ReplayValidationError("recall item is not an object")
            _validate_recall_item_projection(item, index)
            key = _recall_item_key(item, index)
            if key in seen_keys:
                raise ReplayValidationError(f"recall response contains duplicate key: {key}")
            seen_keys.add(key)
            body = _recall_item_body(item, index)
            row = dict(item)
            row["body_json"] = body
            expected_wire_rank = offset + index + 1
            if "wire_rank" in item and item["wire_rank"] != expected_wire_rank:
                raise ReplayValidationError("recall wire ranks are not contiguous for this page")
            row["wire_rank"] = expected_wire_rank
            if "score" in item:
                score = item["score"]
                if score is None:
                    if "score_semantics" in item:
                        raise ReplayValidationError("null score cannot carry score_semantics")
                    row.pop("score", None)
                else:
                    row["score"] = _finite_number(score, f"recall item {index}.score")
                    if "score_semantics" not in item:
                        raise ReplayValidationError("score requires explicit score_semantics")
                    semantics = item["score_semantics"]
                    row["score_semantics"] = _id(semantics, f"recall item {index}.score_semantics")
            elif "score_semantics" in item:
                raise ReplayValidationError("score_semantics requires an explicit score")
            if "decay_score" in item and item["decay_score"] is not None:
                row["decay_score"] = _finite_number(item["decay_score"], f"recall item {index}.decay_score")
            normalized.append(row)
        status, reason = _recall_wire_status(response, len(normalized), total, limit, offset)
        if status == "unavailable":
            return _recall_wire_failure(reason or "recall_unavailable")
        result: dict[str, Any] = {
            "schema_version": RECALL_WIRE_SCHEMA_VERSION,
            "status": status,
            "items": normalized,
            "total": total,
        }
        if profile is not None:
            result["retrieval_profile"] = profile
        if "variants" in response:
            result["variants"] = response["variants"]
        for field in _RECALL_OBJECT_FIELDS | _RECALL_STRING_FIELDS | _RECALL_BOOL_FIELDS | _RECALL_LIST_FIELDS:
            if field in response:
                value = copy.deepcopy(response[field])
                if field == "outcome":
                    value["status"] = value["status"].lower()
                result[field] = value
        if reason:
            result["reason"] = reason
        return result
    except (ReplayValidationError, TypeError, ValueError, OverflowError):
        return _recall_wire_failure()


def require_recall_items(response: Any, *, limit: int) -> list[dict[str, Any]]:
    """Return validated complete wire-order items or raise otherwise."""
    normalized = normalize_recall_response(response, limit=limit)
    if normalized["status"] != "complete":
        raise ReplayValidationError(
            f"recall response is not complete ({normalized['status']})"
        )
    return normalized["items"]


def _binary_commit_marker(binary_path: Path, commit: str) -> str:
    try:
        version = subprocess.run(
            [str(binary_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplayValidationError("benchmark binary provenance cannot be read") from exc
    if version.returncode != 0:
        raise ReplayValidationError("benchmark binary version check failed")
    marker = re.search(r"(?<![0-9a-f])g([0-9a-f]{7,40})(?![0-9a-f])", version.stdout.lower())
    if marker is None or not commit.startswith(marker.group(1)):
        raise ReplayValidationError("benchmark binary is not built from the repository commit")
    return marker.group(1)


def _current_preflight_binding(*, binary: str, repo_root: str, dataset: Any = _UNSET, config: Any = _UNSET) -> dict[str, Any]:
    binary_path = Path(binary).resolve()
    if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
        raise ReplayValidationError("benchmark binary is missing or not executable")
    root = Path(repo_root).resolve()
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReplayValidationError("benchmark source commit cannot be resolved") from exc
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReplayValidationError("benchmark source commit is malformed")
    _binary_commit_marker(binary_path, commit)
    binding = {
        "binary_sha256": hashlib.sha256(binary_path.read_bytes()).hexdigest(),
        "binary_commit": commit,
        "binary_commit_sha256": sha256_text(commit),
        "response_schema": RECALL_WIRE_SCHEMA_VERSION,
        "response_schema_sha256": sha256_text(RECALL_WIRE_SCHEMA_VERSION),
    }
    if dataset is not _UNSET:
        binding["dataset_sha256"] = sha256_text(stable_json(dataset))
    if config is not _UNSET:
        binding["config_sha256"] = sha256_text(stable_json(config))
    return binding


def prepare_recall_preflight(*, binary: str, db_path: str, dataset: Any, config: Any, repo_root: str) -> dict[str, Any]:
    """Bind a benchmark run to the binary, source commit, schema, inputs, and a fresh DB."""
    binding = _current_preflight_binding(
        binary=binary,
        repo_root=repo_root,
        dataset=dataset,
        config=config,
    )
    database_path = Path(db_path).resolve()
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(database_path) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ReplayValidationError("benchmark database could not be reset") from exc
    if any(Path(str(database_path) + suffix).exists() for suffix in ("", "-wal", "-shm", "-journal")):
        raise ReplayValidationError("benchmark database is not fresh")
    try:
        connection = sqlite3.connect(str(database_path))
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise ReplayValidationError("benchmark database could not be initialized") from exc
    database_stat = database_path.stat()
    database_identity = {
        "device": database_stat.st_dev,
        "inode": database_stat.st_ino,
        "ctime_ns": database_stat.st_ctime_ns,
        "size": database_stat.st_size,
    }
    return {
        **binding,
        "database_fresh": True,
        "database_identity": database_identity,
        "database_id_sha256": sha256_text(stable_json(database_identity)),
    }


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
    wire_ranks = [record["wire_rank"] for record in records]
    if sorted(wire_ranks) != list(range(1, len(records) + 1)):
        raise ReplayValidationError("snapshot wire ranks must be contiguous from one")
    positions = [record["original_position"] for record in records]
    if len(set(positions)) != len(positions):
        raise ReplayValidationError("snapshot original positions must be unique")
    canonical = sorted(records, key=lambda row: (row["candidate_id_sha256"], row["wire_rank"]))
    if records != canonical:
        raise ReplayValidationError("snapshot records are not in canonical order")
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
        return sorted(raw, key=lambda row: _hash_identifier(row["candidate_id"]))
    raise ReplayValidationError(f"unsupported sequence policy: {policy}")


def _public_sequence_order(candidates: list[Mapping[str, Any]], policy: str) -> list[Mapping[str, Any]]:
    if policy == "wire_v1":
        return sorted(candidates, key=lambda row: row["wire_rank"])
    if policy == "chronological_sequence_v1":
        return sorted(candidates, key=lambda row: (row["original_position"], row["candidate_id_sha256"]))
    if policy == "identity_v1":
        return sorted(candidates, key=lambda row: row["candidate_id_sha256"])
    raise ReplayValidationError(f"unsupported sequence policy: {policy}")


def _context_digest(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayValidationError(f"{field} must be non-empty")
    return sha256_text(value)


def _validate_preflight(preflight: Any) -> str:
    if not isinstance(preflight, Mapping):
        raise ReplayValidationError("preflight commitment is missing")
    required = {
        "binary_sha256", "binary_commit", "binary_commit_sha256", "database_fresh",
        "database_identity", "database_id_sha256", "response_schema", "response_schema_sha256",
        "dataset_sha256", "config_sha256",
    }
    unknown = set(preflight) - _PREFLIGHT_FIELDS
    if unknown:
        raise ReplayValidationError(f"preflight contains unknown field: {sorted(unknown)[0]}")
    if not required.issubset(preflight):
        raise ReplayValidationError("preflight commitment is incomplete")
    for field in ("binary_sha256", "binary_commit_sha256", "database_id_sha256",
                  "response_schema_sha256", "dataset_sha256", "config_sha256"):
        _sha(preflight[field], f"preflight.{field}")
    if not isinstance(preflight["binary_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", preflight["binary_commit"]):
        raise ReplayValidationError("preflight.binary_commit is malformed")
    if preflight["binary_commit_sha256"] != sha256_text(preflight["binary_commit"]):
        raise ReplayValidationError("preflight binary commit digest mismatch")
    if preflight["database_fresh"] is not True:
        raise ReplayValidationError("preflight database must be fresh")
    identity = preflight["database_identity"]
    if not isinstance(identity, Mapping) or set(identity) != {"device", "inode", "ctime_ns", "size"}:
        raise ReplayValidationError("preflight database identity is malformed")
    for field in ("device", "inode", "ctime_ns", "size"):
        _nonnegative_int(identity[field], f"preflight.database_identity.{field}")
    if preflight["database_id_sha256"] != sha256_text(stable_json(identity)):
        raise ReplayValidationError("preflight database identity digest mismatch")
    if preflight["response_schema"] != RECALL_WIRE_SCHEMA_VERSION:
        raise ReplayValidationError("preflight response schema is unsupported")
    if preflight["response_schema_sha256"] != sha256_text(RECALL_WIRE_SCHEMA_VERSION):
        raise ReplayValidationError("preflight response schema digest mismatch")
    material = {key: preflight[key] for key in sorted(required)}
    return sha256_text(stable_json(material))


def validate_recall_preflight(
    preflight: Any,
    *,
    binary: str | None = None,
    db_path: str | None = None,
    repo_root: str | None = None,
    dataset: Any = _UNSET,
    config: Any = _UNSET,
) -> None:
    """Validate a persisted preflight before reusing a measured cell.

    With runtime inputs supplied, recompute the executable, source commit,
    dataset, and config commitments instead of trusting values copied from a
    mutable journal.
    """
    _validate_preflight(preflight)
    has_runtime = any(value is not None for value in (binary, db_path, repo_root))
    if not has_runtime and dataset is _UNSET and config is _UNSET:
        return
    if binary is None or db_path is None or repo_root is None:
        raise ReplayValidationError("preflight runtime binding is incomplete")
    expected = _current_preflight_binding(
        binary=binary,
        repo_root=repo_root,
        dataset=dataset,
        config=config,
    )
    for field in _PREFLIGHT_BINDING_FIELDS:
        if preflight[field] != expected[field]:
            raise ReplayValidationError(f"preflight {field} differs from current runtime")
    for field in ("dataset_sha256", "config_sha256"):
        if field in expected and preflight[field] != expected[field]:
            raise ReplayValidationError(f"preflight {field} differs from current runtime")
    database_path = Path(db_path).resolve()
    try:
        database_stat = database_path.stat()
    except OSError as exc:
        raise ReplayValidationError("preflight database is not available at runtime") from exc
    identity = preflight["database_identity"]
    if (
        database_stat.st_dev != identity["device"]
        or database_stat.st_ino != identity["inode"]
    ):
        raise ReplayValidationError("preflight database identity differs from current runtime")
    if (dataset is not _UNSET) != ("dataset_sha256" in preflight):
        raise ReplayValidationError("preflight dataset binding is incomplete")
    if (config is not _UNSET) != ("config_sha256" in preflight):
        raise ReplayValidationError("preflight config binding is incomplete")


def _validate_runtime_preflight(preflight: Mapping[str, Any], runtime_binding: Mapping[str, Any]) -> None:
    if set(runtime_binding) != _RUNTIME_BINDING_FIELDS:
        raise ReplayValidationError("runtime preflight binding is incomplete")
    validate_recall_preflight(
        preflight,
        binary=runtime_binding["binary"],
        db_path=runtime_binding["db_path"],
        repo_root=runtime_binding["repo_root"],
        dataset=runtime_binding["dataset"],
        config=runtime_binding["config"],
    )


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
    preflight: Mapping[str, Any],
    context_policy: str,
    context_policy_version: str,
    snapshot: dict[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    sequence_policy: str = "wire_v1",
    status: str | None = None,
    reason: str | None = None,
    runtime_binding: Mapping[str, Any] | None = None,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    """Create a deterministic public envelope from internal retrieval rows."""
    if runtime_binding is None:
        if not allow_synthetic:
            raise ReplayValidationError("runtime binding is required for replay publication")
    else:
        _validate_runtime_preflight(preflight, runtime_binding)
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
    preflight_sha256 = _validate_preflight(preflight)
    if preflight["dataset_sha256"] != corpus_sha256:
        raise ReplayValidationError("preflight dataset differs from envelope corpus")
    if preflight["config_sha256"] != config_sha256:
        raise ReplayValidationError("preflight config differs from envelope config")
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
        "commitments": {
            "config_sha256": config_sha256,
            "code_sha256": code_sha256,
            "preflight_sha256": preflight_sha256,
        },
        "binding_mode": "runtime" if runtime_binding is not None else "synthetic",
        "preflight": copy.deepcopy(dict(preflight)),
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
        "preflight": envelope_without_hashes["preflight"],
    }
    return sha256_text(stable_json(material))


def validate_envelope(envelope: Any) -> None:
    if not isinstance(envelope, dict):
        raise ReplayValidationError("envelope must be an object")
    allowed = {
        "schema_version", "workspace_sha256", "scope_sha256", "fixture_id", "corpus_sha256",
        "snapshot_sha256", "retrieval", "request", "commitments", "preflight", "context_policy", "status",
        "reason", "membership", "candidates", "raw_inputs_captured", "network_calls",
        "binding_mode", "replay_fingerprint_sha256", "projection_sha256",
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
    if not isinstance(commitments, dict) or set(commitments) != {
        "config_sha256", "code_sha256", "preflight_sha256"
    }:
        raise ReplayValidationError("code/config commitments are malformed")
    _sha(commitments["config_sha256"], "commitments.config_sha256")
    _sha(commitments["code_sha256"], "commitments.code_sha256")
    if "preflight_sha256" in commitments:
        _sha(commitments["preflight_sha256"], "commitments.preflight_sha256")
    if "preflight" not in envelope:
        raise ReplayValidationError("preflight commitment is missing")
    preflight_sha256 = _validate_preflight(envelope["preflight"])
    if commitments.get("preflight_sha256") != preflight_sha256:
        raise ReplayValidationError("preflight commitment digest mismatch")
    if envelope["preflight"].get("dataset_sha256") != envelope["corpus_sha256"]:
        raise ReplayValidationError("preflight dataset differs from envelope corpus")
    if envelope["preflight"].get("config_sha256") != commitments["config_sha256"]:
        raise ReplayValidationError("preflight config differs from envelope config")
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
    if envelope.get("binding_mode") not in {"runtime", "synthetic"}:
        raise ReplayValidationError("binding mode is invalid")
    if status in {"degraded", "partial", "unavailable"}:
        _id(envelope.get("reason"), "reason")
    elif "reason" in envelope:
        raise ReplayValidationError("reason is only valid for degraded/partial/unavailable states")
    membership = envelope.get("membership")
    if not isinstance(membership, dict) or set(membership) != {"candidate_count", "delivered_count", "requested_top_k", "complete", "truncated"}:
        raise ReplayValidationError("membership is malformed")
    candidate_count = _nonnegative_int(membership["candidate_count"], "membership.candidate_count")
    delivered_count = _nonnegative_int(membership["delivered_count"], "membership.delivered_count")
    if candidate_count < delivered_count:
        raise ReplayValidationError("membership candidate count is below delivered count")
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
    expected_order = _public_sequence_order(candidates, retrieval["sequence_policy"])
    if [row["candidate_id_sha256"] for row in candidates] != [row["candidate_id_sha256"] for row in expected_order]:
        raise ReplayValidationError("delivered candidate order violates sequence policy")
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


def replay_envelope(
    envelope: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    runtime_binding: Mapping[str, Any] | None = None,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    """Validate membership/order against a hash-only snapshot."""
    validate_envelope(dict(envelope))
    if envelope["binding_mode"] == "runtime":
        if runtime_binding is None:
            raise ReplayValidationError("runtime binding is required to replay this envelope")
        _validate_runtime_preflight(envelope["preflight"], runtime_binding)
    elif not allow_synthetic:
        raise ReplayValidationError("synthetic replay cannot be used for publication")
    validate_snapshot(dict(snapshot))
    if envelope["snapshot_sha256"] != snapshot["snapshot_sha256"]:
        raise ReplayValidationError("envelope snapshot commitment mismatch")
    if envelope["membership"]["candidate_count"] != len(snapshot["records"]):
        raise ReplayValidationError("envelope candidate count differs from snapshot")
    by_id = {record["candidate_id_sha256"]: record for record in snapshot["records"]}
    expected = _public_sequence_order(snapshot["records"], envelope["retrieval"]["sequence_policy"])
    expected = expected[: envelope["retrieval"]["top_k"]]
    if [candidate["candidate_id_sha256"] for candidate in envelope["candidates"]] != [
        record["candidate_id_sha256"] for record in expected
    ]:
        raise ReplayValidationError("envelope membership/order differs from snapshot sequence")
    for index, candidate in enumerate(envelope["candidates"]):
        record = by_id.get(candidate["candidate_id_sha256"])
        if record is None:
            raise ReplayValidationError(f"candidate {index} is absent from snapshot")
        for field in ("candidate_id_sha256", "source_ref_sha256", "content_sha256", "content_chars", "provenance_sha256", "wire_rank", "original_position", "score", "score_semantics"):
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
    "ReplayValidationError", "SCHEMA_VERSION", "SNAPSHOT_SCHEMA_VERSION", "RECALL_WIRE_SCHEMA_VERSION",
    "build_envelope", "build_snapshot", "normalize_recall_response", "recall_status_is_scoreable", "require_recall_items", "prepare_recall_preflight",
    "validate_recall_preflight",
    "replay_envelope", "sha256_text", "stable_json", "validate_envelope", "validate_snapshot",
]
