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
import secrets
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
_MAX_RECALL_LIMIT = 1000
_MAX_RECALL_OFFSET = 10000
_MAX_TIMESTAMP_UNIX_MS = 253402300799999


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
    "key", "id", "body_json", "category", "status", "type", "tags", "score", "score_semantics",
    "decay_score", "why_served", "wire_rank", "retrieval_count", "layer", "topic_path", "archived",
    "archive_reason", "links", "verified", "source", "always_on", "certainty", "workspace_hash",
    "agent_id", "visibility", "created_at_unix_ms", "updated_at_unix_ms", "last_accessed_unix_ms", "follow_count", "miss_count",
    "follow_rate", "efficacy_status", "epistemic_state", "hints", "memory_type", "encoding_strength",
    "content", "summary", "metadata", "untrusted", "untrusted_reason", "validity", "context_invalid",
    "as_of_unix_ms", "is_live_version", "recorded_at_unix_ms", "valid_from_unix_ms", "valid_to_unix_ms",
    "confidence", "confirmed_query_key", "provider_source",
})
_WHY_SERVED_FIELDS = frozenset({
    "reason", "memory_class", "promotion_state", "support_count", "source_evidence_ids", "promoted_scope",
})
_PREFLIGHT_FIELDS = frozenset({
    "binary_sha256", "binary_commit", "binary_commit_sha256", "database_fresh",
    "database_identity", "database_id_sha256", "database_attestation_sha256",
    "response_schema", "response_schema_sha256", "dataset_sha256", "config_sha256",
})
_DATABASE_ATTESTATION_TABLE = "perseus_recall_preflight"
_DATABASE_ATTESTATION_SCHEMA = "perseus-recall-preflight/v1"
_DATABASE_APPLICATION_ID = 0x50524631
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


_RECALL_DIAGNOSTIC_FIELDS = frozenset({
    "reason", "hint", "active_memories", "embedded_memories", "semantic_recall",
})
_RECALL_OUTCOME_FIELDS = frozenset({
    "status", "abstained", "reason", "deadline_elapsed", "backend_health",
    "completeness", "candidate_scope",
})
_RECALL_BACKEND_HEALTH_FIELDS = frozenset({
    "enabled", "query_embedding_available", "embedded_memories", "active_memories",
    "pending_embed_jobs",
})
_RECALL_COMPLETENESS_FIELDS = frozenset({"completeness", "scope", "degraded"})
_RECALL_SCOPE_FIELDS = frozenset({"scanned", "total_embedded", "embedded_population", "pool_bound"})
_RECALL_FRESHNESS_SUMMARY_FIELDS = frozenset({"fresh", "expired", "never_verified"})
_RECALL_FRESHNESS_GATE_FIELDS = frozenset({
    "proceed", "verdict", "reason", "expired_ids", "note",
})
_RECALL_GRAPH_FIELDS = frozenset({
    "schema_version", "workspace_hash", "source_key", "nodes", "edges", "truncated",
})
_RECALL_GRAPH_NODE_FIELDS = frozenset({
    "node_id", "namespace", "canonical_id", "node_type", "external_ref", "workspace_hash", "state",
})
_RECALL_GRAPH_EDGE_FIELDS = frozenset({
    "edge_id", "manifest_id", "source_id", "source_key", "source_revision", "source_sha256",
    "manifest_span_ref", "from_node_id", "to_node_id", "from", "to", "predicate", "direction",
    "context", "source_span_ref", "workspace_hash", "origin", "attestation_state", "attested_by",
    "attestation_ref", "valid_from_unix_ms", "valid_to_unix_ms", "state", "recorded_at_unix_ms",
})
_RECALL_EVIDENCE_FIELDS = frozenset({"status", "lanes", "items", "budget", "excluded", "receipt"})
_RECALL_EVIDENCE_ITEM_FIELDS = frozenset({
    "lane", "entity_id", "source", "span", "source_groups", "chain_identity", "verification",
    "trust", "tokens", "revision", "span_sha256", "text",
})
_RECALL_SOURCE_FIELDS = frozenset({"id", "category", "key", "revision"})
_RECALL_SPAN_FIELDS = frozenset({"start_char", "end_char"})
_RECALL_CHAIN_RECEIPT_FIELDS = frozenset({"status", "commitment_sha256"})
_RECALL_EVIDENCE_BUDGET_FIELDS = frozenset({"max_tokens", "selected_tokens", "omitted_tokens", "per_lane"})
_RECALL_LANE_BUDGET_FIELDS = frozenset({"lane", "selected_items", "omitted_items", "selected_tokens", "omitted_tokens"})
_RECALL_RECEIPT_FIELDS = frozenset({
    "schema_version", "query_sha256", "lanes", "max_tokens", "workspace_hash",
    "requesting_agent_id", "as_of_unix_ms", "valid_at", "selected", "excluded", "budget", "digest",
    "requirement_sha256", "candidate_set_sha256", "selected_set_sha256", "omitted_set_sha256", "reasons",
})
_RECALL_RECEIPT_SELECTION_FIELDS = frozenset({
    "lane", "entity_id", "source_groups", "chain_identity", "revision", "span_sha256",
    "verification", "trust", "tokens",
})
_RECALL_RECEIPT_REASON_FIELDS = frozenset({"reason", "count"})
_RECALL_CONFLICT_FIELDS = frozenset({
    "candidate_id", "claim_id", "kind", "validity", "evidence_refs", "confidence", "disposition",
    "disclose_existence", "disclose_value",
})
_RECALL_CONFLICT_VALIDITY_FIELDS = frozenset({
    "valid_from_unix_ms", "valid_to_unix_ms", "recorded_at_unix_ms", "invalidated_at_unix_ms",
})
_RECALL_CONFLICT_REF_FIELDS = frozenset({"entity_id", "card_digest"})
_RECALL_FUSED_FIELDS = frozenset({
    "original_query", "expansions", "strategies", "fusion", "truncation", "rerank", "placement",
    "state_filters", "sources", "graph_route", "validity", "anchor_matched", "multihop",
    "source_chain_exclusions", "selection_decisions",
})
_RECALL_FUSED_STRATEGY_FIELDS = frozenset({"strategy", "candidates", "top", "status", "latency_ms"})
_RECALL_FUSED_FUSION_FIELDS = frozenset({"rrf_k", "weights", "fused_count"})
_RECALL_FUSED_TRUNCATION_FIELDS = frozenset({
    "budget_tokens", "estimated_tokens_used", "retained", "dropped", "per_type",
})
_RECALL_FUSED_TYPE_REPORT_FIELDS = frozenset({"profile", "allocations"})
_RECALL_FUSED_ALLOCATION_FIELDS = frozenset({"class", "floor", "cap", "retained", "floor_shortfall"})
_RECALL_FUSED_RERANK_FIELDS = frozenset({"enabled", "applied", "method", "note"})
_RECALL_GRAPH_ROUTE_FIELDS = frozenset({
    "utility", "reason", "selected", "skipped_reason", "unattested_edges_skipped",
    "out_of_scope_edges_skipped", "expired_targets_skipped", "dangling_targets_skipped",
})
_RECALL_VALIDITY_FIELDS = frozenset({"profile", "method", "weights", "grade_counts", "flagged_context_invalid"})
_RECALL_VALIDITY_WEIGHT_FIELDS = frozenset({
    "freshness_half_life_secs", "scope_bonus", "provenance_boost", "superseded_penalty",
    "expiring_penalty", "stale_freshness", "context_invalid_freshness",
})
_RECALL_MULTIHOP_FIELDS = frozenset({
    "hop_expanded", "expanded_ids", "selection_order", "covered_entities", "uncovered_entities",
})
_RECALL_CHAIN_EXCLUSION_FIELDS = frozenset({"reason", "count"})
_RECALL_SELECTION_TRACE_FIELDS = frozenset({
    "schema_version", "policy_digest", "arms", "candidate_count", "eligible_count", "retained_count",
    "delivered_count", "abstained", "abstention_reason", "token_budget", "estimated_tokens_used",
    "candidates", "delivered_order", "replay_fingerprint_sha256",
})
_RECALL_SELECTION_ARM_FIELDS = frozenset({"arm", "status", "candidate_count"})
_RECALL_SELECTION_FIELDS = frozenset({
    "candidate_id", "source_chain_commitment", "source_chain_status", "source_arm_ranks", "fused_rank",
    "fused_score", "rerank_score", "validity_multiplier", "token_estimate", "token_estimator", "eligible",
    "selected", "final_rank", "disposition",
})
_RECALL_FORBIDDEN_PROJECTION_KEYS = frozenset({
    "raw_query", "raw_prompt", "prompt", "raw_body", "raw_payload", "private_projection",
    "secret", "credential", "access_token", "api_key",
})


def _projection_object(value: Any, field: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayValidationError(f"{field} must be an object when present")
    unknown = set(value) - allowed
    if unknown or any(key in _RECALL_FORBIDDEN_PROJECTION_KEYS for key in value):
        raise ReplayValidationError(f"{field} contains an unknown nested field")
    return value


def _require_projection_fields(obj: Mapping[str, Any], field: str, required: frozenset[str]) -> None:
    missing = required - set(obj)
    if missing:
        raise ReplayValidationError(f"{field} is missing required field: {sorted(missing)[0]}")


def _projection_text(value: Any, field: str, *, max_chars: int = 4096) -> None:
    if not isinstance(value, str) or len(value) > max_chars:
        raise ReplayValidationError(f"{field} must be bounded text")


def _projection_integer(value: Any, field: str, *, nonnegative: bool = True) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or (nonnegative and value < 0):
        raise ReplayValidationError(f"{field} must be an integer")


def _projection_number(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ReplayValidationError(f"{field} must be a finite number")


def _projection_boolean(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        raise ReplayValidationError(f"{field} must be a boolean")


def _projection_array(value: Any, field: str, validator: Any = None, *, max_items: int = 4096) -> None:
    if not isinstance(value, list) or len(value) > max_items:
        raise ReplayValidationError(f"{field} must be a bounded list")
    if validator is not None:
        for index, item in enumerate(value):
            validator(item, f"{field}[{index}]")


def _projection_sha(value: Any, field: str) -> None:
    _sha(value, field)


def _projection_string_map(value: Any, field: str, *, item_validator: Any = None) -> None:
    if not isinstance(value, Mapping) or len(value) > 4096:
        raise ReplayValidationError(f"{field} must be a bounded object map")
    for key, item in value.items():
        _projection_text(key, f"{field} key", max_chars=256)
        if item_validator is not None:
            item_validator(item, f"{field}.{key}")


def _validate_recall_diagnostic(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_DIAGNOSTIC_FIELDS)
    for key in ("reason", "hint", "semantic_recall"):
        if key in obj:
            _projection_text(obj[key], f"{field}.{key}")
    for key in ("active_memories", "embedded_memories"):
        if key in obj:
            _projection_integer(obj[key], f"{field}.{key}")


def _validate_recall_outcome(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_OUTCOME_FIELDS)
    _require_projection_fields(obj, field, frozenset({"status"}))
    if "status" in obj:
        status = obj["status"]
        if not isinstance(status, str) or status.lower() not in _RECALL_OUTCOME_STATUS:
            raise ReplayValidationError(f"{field}.status is invalid")
    for key in ("abstained", "deadline_elapsed"):
        if key in obj:
            _projection_boolean(obj[key], f"{field}.{key}")
    if "reason" in obj:
        _projection_text(obj["reason"], f"{field}.reason")
    if "backend_health" in obj:
        health = _projection_object(obj["backend_health"], f"{field}.backend_health", _RECALL_BACKEND_HEALTH_FIELDS)
        for key in ("enabled", "query_embedding_available"):
            if key in health:
                _projection_boolean(health[key], f"{field}.backend_health.{key}")
        for key in ("embedded_memories", "active_memories", "pending_embed_jobs"):
            if key in health:
                _projection_integer(health[key], f"{field}.backend_health.{key}")
    if "completeness" in obj:
        completeness = _projection_object(obj["completeness"], f"{field}.completeness", _RECALL_COMPLETENESS_FIELDS)
        if "completeness" in completeness:
            _projection_text(completeness["completeness"], f"{field}.completeness.completeness", max_chars=32)
        if "degraded" in completeness:
            _projection_text(completeness["degraded"], f"{field}.completeness.degraded")
        if "scope" in completeness:
            scope = _projection_object(completeness["scope"], f"{field}.completeness.scope", _RECALL_SCOPE_FIELDS)
            for key in _RECALL_SCOPE_FIELDS:
                if key in scope and scope[key] is not None:
                    _projection_integer(scope[key], f"{field}.completeness.scope.{key}")


def _validate_recall_freshness_summary(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_FRESHNESS_SUMMARY_FIELDS)
    for key in _RECALL_FRESHNESS_SUMMARY_FIELDS:
        if key in obj:
            _projection_integer(obj[key], f"{field}.{key}")


def _validate_recall_freshness_gate(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_FRESHNESS_GATE_FIELDS)
    if "proceed" in obj:
        _projection_boolean(obj["proceed"], f"{field}.proceed")
    for key in ("verdict", "reason", "note"):
        if key in obj:
            _projection_text(obj[key], f"{field}.{key}")
    if "expired_ids" in obj:
        _projection_array(obj["expired_ids"], f"{field}.expired_ids", lambda item, path: _projection_text(item, path, max_chars=256))


def _validate_recall_graph_node(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_GRAPH_NODE_FIELDS)
    for key in obj:
        if key == "external_ref":
            if obj[key] is not None:
                _projection_text(obj[key], f"{field}.{key}")
        else:
            _projection_text(obj[key], f"{field}.{key}")


def _validate_recall_graph_edge(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_GRAPH_EDGE_FIELDS)
    for key, item in obj.items():
        if key.endswith("_sha256"):
            _projection_sha(item, f"{field}.{key}")
        elif key.endswith("_unix_ms"):
            if item is not None:
                _projection_integer(item, f"{field}.{key}", nonnegative=False)
        elif key in {"manifest_span_ref", "context", "source_span_ref", "attested_by", "attestation_ref"}:
            if item is not None:
                _projection_text(item, f"{field}.{key}")
        else:
            _projection_text(item, f"{field}.{key}")


def _validate_recall_declared_graph(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_GRAPH_FIELDS)
    _require_projection_fields(obj, field, frozenset({"nodes", "edges"}))
    for key in ("workspace_hash", "source_key"):
        if key in obj and obj[key] is not None:
            _projection_text(obj[key], f"{field}.{key}", max_chars=256)
    if "schema_version" in obj:
        _projection_integer(obj["schema_version"], f"{field}.schema_version")
    if "truncated" in obj:
        _projection_boolean(obj["truncated"], f"{field}.truncated")
    if "nodes" in obj:
        _projection_array(obj["nodes"], f"{field}.nodes", _validate_recall_graph_node)
    if "edges" in obj:
        _projection_array(obj["edges"], f"{field}.edges", _validate_recall_graph_edge)


def _validate_recall_chain_receipt(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_CHAIN_RECEIPT_FIELDS)
    if "status" in obj:
        _projection_text(obj["status"], f"{field}.status", max_chars=32)
    if "commitment_sha256" in obj:
        _projection_sha(obj["commitment_sha256"], f"{field}.commitment_sha256")
    if obj.get("status") == "unknown" and "commitment_sha256" in obj:
        raise ReplayValidationError(f"{field}.unknown identity cannot carry a commitment")


def _validate_recall_evidence_item(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_EVIDENCE_ITEM_FIELDS)
    for key in ("lane", "verification", "trust", "revision"):
        if key in obj:
            _projection_text(obj[key], f"{field}.{key}", max_chars=128)
    for key in ("entity_id", "text"):
        if key in obj and obj[key] is not None:
            _projection_text(obj[key], f"{field}.{key}")
    if "source" in obj and obj["source"] is not None:
        source = _projection_object(obj["source"], f"{field}.source", _RECALL_SOURCE_FIELDS)
        for key, item in source.items():
            _projection_text(item, f"{field}.source.{key}", max_chars=512)
    if "span" in obj and obj["span"] is not None:
        span = _projection_object(obj["span"], f"{field}.span", _RECALL_SPAN_FIELDS)
        for key, item in span.items():
            _projection_integer(item, f"{field}.span.{key}")
    if "source_groups" in obj:
        _projection_array(obj["source_groups"], f"{field}.source_groups", lambda item, path: _projection_text(item, path, max_chars=256))
    if "chain_identity" in obj:
        _validate_recall_chain_receipt(obj["chain_identity"], f"{field}.chain_identity")
    if "tokens" in obj:
        _projection_integer(obj["tokens"], f"{field}.tokens")
    if "span_sha256" in obj:
        _projection_sha(obj["span_sha256"], f"{field}.span_sha256")


def _validate_recall_lane_budget(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_LANE_BUDGET_FIELDS)
    if "lane" in obj:
        _projection_text(obj["lane"], f"{field}.lane", max_chars=32)
    for key in ("selected_items", "omitted_items", "selected_tokens", "omitted_tokens"):
        if key in obj:
            _projection_integer(obj[key], f"{field}.{key}")


def _validate_recall_evidence_budget(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_EVIDENCE_BUDGET_FIELDS)
    for key in ("max_tokens", "selected_tokens", "omitted_tokens"):
        if key in obj:
            _projection_integer(obj[key], f"{field}.{key}")
    if "per_lane" in obj:
        _projection_array(obj["per_lane"], f"{field}.per_lane", _validate_recall_lane_budget)


def _validate_recall_receipt_selection(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_RECEIPT_SELECTION_FIELDS)
    for key in ("lane", "entity_id", "revision", "verification", "trust"):
        if key in obj:
            _projection_text(obj[key], f"{field}.{key}", max_chars=512)
    if "source_groups" in obj:
        _projection_array(obj["source_groups"], f"{field}.source_groups", lambda item, path: _projection_text(item, path, max_chars=256))
    if "chain_identity" in obj:
        _validate_recall_chain_receipt(obj["chain_identity"], f"{field}.chain_identity")
    if "span_sha256" in obj:
        _projection_sha(obj["span_sha256"], f"{field}.span_sha256")
    if "tokens" in obj:
        _projection_integer(obj["tokens"], f"{field}.tokens")


def _validate_recall_receipt(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_RECEIPT_FIELDS)
    for key in ("query_sha256", "requirement_sha256", "candidate_set_sha256", "selected_set_sha256", "omitted_set_sha256", "digest"):
        if key in obj:
            _projection_sha(obj[key], f"{field}.{key}")
    if "schema_version" in obj and not isinstance(obj["schema_version"], (str, int)):
        raise ReplayValidationError(f"{field}.schema_version is malformed")
    for key in ("workspace_hash", "requesting_agent_id"):
        if key in obj and obj[key] is not None:
            _projection_text(obj[key], f"{field}.{key}", max_chars=256)
    for key in ("as_of_unix_ms", "valid_at", "max_tokens"):
        if key in obj and obj[key] is not None:
            _projection_integer(obj[key], f"{field}.{key}", nonnegative=key == "max_tokens")
    if "lanes" in obj:
        _projection_array(obj["lanes"], f"{field}.lanes", lambda item, path: _projection_text(item, path, max_chars=32))
    if "selected" in obj:
        _projection_array(obj["selected"], f"{field}.selected", _validate_recall_receipt_selection)
    if "excluded" in obj:
        _projection_array(obj["excluded"], f"{field}.excluded", lambda item, path: _validate_recall_receipt_reason(item, path))
    if "reasons" in obj:
        _projection_array(obj["reasons"], f"{field}.reasons", _validate_recall_receipt_reason)
    if "budget" in obj:
        _validate_recall_evidence_budget(obj["budget"], f"{field}.budget")


def _validate_recall_receipt_reason(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_RECEIPT_REASON_FIELDS)
    if "reason" in obj:
        _projection_text(obj["reason"], f"{field}.reason", max_chars=256)
    if "count" in obj:
        _projection_integer(obj["count"], f"{field}.count")


def _validate_recall_evidence(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_EVIDENCE_FIELDS)
    _require_projection_fields(obj, field, frozenset({"status"}))
    if "status" in obj:
        _projection_text(obj["status"], f"{field}.status", max_chars=64)
    if "lanes" in obj:
        _projection_array(obj["lanes"], f"{field}.lanes", lambda item, path: _projection_text(item, path, max_chars=32))
    if "items" in obj:
        _projection_array(obj["items"], f"{field}.items", _validate_recall_evidence_item)
    if "budget" in obj:
        _validate_recall_evidence_budget(obj["budget"], f"{field}.budget")
    if "excluded" in obj:
        _projection_array(obj["excluded"], f"{field}.excluded", _validate_recall_receipt_reason)
    if "receipt" in obj:
        _validate_recall_receipt(obj["receipt"], f"{field}.receipt")


def _validate_recall_conflict(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_CONFLICT_FIELDS)
    for key in ("candidate_id", "claim_id", "kind", "confidence", "disposition"):
        if key in obj:
            _projection_text(obj[key], f"{field}.{key}", max_chars=256)
    for key in ("disclose_existence", "disclose_value"):
        if key in obj:
            _projection_boolean(obj[key], f"{field}.{key}")
    if "validity" in obj:
        validity = _projection_object(obj["validity"], f"{field}.validity", frozenset({"candidate", "claim"}))
        for side, item in validity.items():
            values = _projection_object(item, f"{field}.validity.{side}", _RECALL_CONFLICT_VALIDITY_FIELDS)
            for key, timestamp in values.items():
                if timestamp is not None:
                    _projection_integer(timestamp, f"{field}.validity.{side}.{key}", nonnegative=False)
    if "evidence_refs" in obj:
        def validate_ref(item: Any, path: str) -> None:
            ref = _projection_object(item, path, _RECALL_CONFLICT_REF_FIELDS)
            _require_projection_fields(ref, path, frozenset({"entity_id", "card_digest"}))
            _projection_text(ref["entity_id"], f"{path}.entity_id", max_chars=256)
            _projection_sha(ref["card_digest"], f"{path}.card_digest")
        _projection_array(obj["evidence_refs"], f"{field}.evidence_refs", validate_ref)


def _validate_recall_selection_decision(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_SELECTION_FIELDS)
    for key in ("candidate_id", "source_chain_status", "token_estimator", "disposition"):
        if key in obj:
            _projection_text(obj[key], f"{field}.{key}", max_chars=256)
    if "source_chain_commitment" in obj and obj["source_chain_commitment"] is not None:
        _projection_sha(obj["source_chain_commitment"], f"{field}.source_chain_commitment")
    if "source_arm_ranks" in obj:
        _projection_string_map(obj["source_arm_ranks"], f"{field}.source_arm_ranks", item_validator=lambda item, path: _projection_integer(item, path, nonnegative=False))
    for key in ("fused_rank", "token_estimate", "final_rank"):
        if key in obj and obj[key] is not None:
            _projection_integer(obj[key], f"{field}.{key}")
    for key in ("fused_score", "rerank_score", "validity_multiplier"):
        if key in obj and obj[key] is not None:
            _projection_number(obj[key], f"{field}.{key}")
    for key in ("eligible", "selected"):
        if key in obj:
            _projection_boolean(obj[key], f"{field}.{key}")


def _validate_recall_fused(value: Any, field: str) -> None:
    obj = _projection_object(value, field, _RECALL_FUSED_FIELDS)
    if "original_query" in obj:
        _projection_text(obj["original_query"], f"{field}.original_query")
    for key in ("expansions", "placement", "state_filters", "anchor_matched"):
        if key in obj:
            _projection_array(obj[key], f"{field}.{key}", lambda item, path: _projection_text(item, path, max_chars=512))
    if "strategies" in obj:
        def validate_strategy(item: Any, path: str) -> None:
            strategy = _projection_object(item, path, _RECALL_FUSED_STRATEGY_FIELDS)
            for key in ("strategy", "status"):
                if key in strategy:
                    _projection_text(strategy[key], f"{path}.{key}", max_chars=64)
            for key in ("candidates", "latency_ms"):
                if key in strategy:
                    _projection_number(strategy[key], f"{path}.{key}")
            if "top" in strategy:
                _projection_array(strategy["top"], f"{path}.top", lambda value, item_path: _projection_text(value, item_path, max_chars=256))
        _projection_array(obj["strategies"], f"{field}.strategies", validate_strategy)
    if "fusion" in obj:
        fusion = _projection_object(obj["fusion"], f"{field}.fusion", _RECALL_FUSED_FUSION_FIELDS)
        for key in ("rrf_k",):
            if key in fusion:
                _projection_number(fusion[key], f"{field}.fusion.{key}")
        if "fused_count" in fusion:
            _projection_integer(fusion["fused_count"], f"{field}.fusion.fused_count")
        if "weights" in fusion:
            _projection_string_map(fusion["weights"], f"{field}.fusion.weights", item_validator=_projection_number)
    if "truncation" in obj:
        truncation = _projection_object(obj["truncation"], f"{field}.truncation", _RECALL_FUSED_TRUNCATION_FIELDS)
        for key in ("budget_tokens", "estimated_tokens_used", "retained", "dropped"):
            if key in truncation:
                _projection_integer(truncation[key], f"{field}.truncation.{key}")
        if "per_type" in truncation:
            report = _projection_object(truncation["per_type"], f"{field}.truncation.per_type", _RECALL_FUSED_TYPE_REPORT_FIELDS)
            if "profile" in report:
                _projection_text(report["profile"], f"{field}.truncation.per_type.profile", max_chars=64)
            if "allocations" in report:
                def validate_allocation(item: Any, path: str) -> None:
                    allocation = _projection_object(item, path, _RECALL_FUSED_ALLOCATION_FIELDS)
                    if "class" in allocation:
                        _projection_text(allocation["class"], f"{path}.class", max_chars=64)
                    for key in ("floor", "cap", "retained", "floor_shortfall"):
                        if key in allocation:
                            _projection_integer(allocation[key], f"{path}.{key}")
                _projection_array(report["allocations"], f"{field}.truncation.per_type.allocations", validate_allocation)
    if "rerank" in obj:
        rerank = _projection_object(obj["rerank"], f"{field}.rerank", _RECALL_FUSED_RERANK_FIELDS)
        for key in ("enabled", "applied"):
            if key in rerank:
                _projection_boolean(rerank[key], f"{field}.rerank.{key}")
        for key in ("method", "note"):
            if key in rerank:
                _projection_text(rerank[key], f"{field}.rerank.{key}")
    if "sources" in obj:
        _projection_string_map(obj["sources"], f"{field}.sources", item_validator=lambda item, path: _projection_array(item, path, lambda value, item_path: _projection_text(value, item_path, max_chars=64)))
    if "graph_route" in obj:
        route = _projection_object(obj["graph_route"], f"{field}.graph_route", _RECALL_GRAPH_ROUTE_FIELDS)
        for key in ("utility",):
            if key in route:
                _projection_number(route[key], f"{field}.graph_route.{key}")
        for key in ("selected",):
            if key in route:
                _projection_boolean(route[key], f"{field}.graph_route.{key}")
        for key in ("reason", "skipped_reason"):
            if key in route:
                _projection_text(route[key], f"{field}.graph_route.{key}")
        for key in _RECALL_GRAPH_ROUTE_FIELDS - {"utility", "selected", "reason", "skipped_reason"}:
            if key in route:
                _projection_integer(route[key], f"{field}.graph_route.{key}")
    if "validity" in obj:
        validity = _projection_object(obj["validity"], f"{field}.validity", _RECALL_VALIDITY_FIELDS)
        for key in ("profile", "method"):
            if key in validity:
                _projection_text(validity[key], f"{field}.validity.{key}")
        if "weights" in validity:
            weights = _projection_object(validity["weights"], f"{field}.validity.weights", _RECALL_VALIDITY_WEIGHT_FIELDS)
            for key, item in weights.items():
                _projection_number(item, f"{field}.validity.weights.{key}")
        if "grade_counts" in validity:
            _projection_string_map(validity["grade_counts"], f"{field}.validity.grade_counts", item_validator=_projection_integer)
        if "flagged_context_invalid" in validity:
            _projection_integer(validity["flagged_context_invalid"], f"{field}.validity.flagged_context_invalid")
    if "multihop" in obj and obj["multihop"] is not None:
        multihop = _projection_object(obj["multihop"], f"{field}.multihop", _RECALL_MULTIHOP_FIELDS)
        if "hop_expanded" in multihop:
            _projection_integer(multihop["hop_expanded"], f"{field}.multihop.hop_expanded")
        for key in ("expanded_ids", "selection_order", "covered_entities", "uncovered_entities"):
            if key in multihop:
                _projection_array(multihop[key], f"{field}.multihop.{key}", lambda item, path: _projection_text(item, path, max_chars=256))
    if "source_chain_exclusions" in obj:
        def validate_exclusion(item: Any, path: str) -> None:
            exclusion = _projection_object(item, path, _RECALL_CHAIN_EXCLUSION_FIELDS)
            _require_projection_fields(exclusion, path, frozenset({"reason", "count"}))
            _projection_text(exclusion["reason"], f"{path}.reason", max_chars=128)
            _projection_integer(exclusion["count"], f"{path}.count")
        _projection_array(obj["source_chain_exclusions"], f"{field}.source_chain_exclusions", validate_exclusion)
    if "selection_decisions" in obj:
        trace = _projection_object(obj["selection_decisions"], f"{field}.selection_decisions", _RECALL_SELECTION_TRACE_FIELDS)
        _require_projection_fields(
            trace,
            f"{field}.selection_decisions",
            frozenset({"schema_version", "policy_digest", "arms", "candidates", "delivered_order"}),
        )
        for key in ("schema_version", "policy_digest", "abstention_reason"):
            if key in trace and trace[key] is not None:
                if key == "policy_digest":
                    _projection_sha(trace[key], f"{field}.selection_decisions.{key}")
                else:
                    _projection_text(trace[key], f"{field}.selection_decisions.{key}")
        for key in ("candidate_count", "eligible_count", "retained_count", "delivered_count", "token_budget", "estimated_tokens_used"):
            if key in trace:
                _projection_integer(trace[key], f"{field}.selection_decisions.{key}")
        if "abstained" in trace:
            _projection_boolean(trace["abstained"], f"{field}.selection_decisions.abstained")
        if "arms" in trace:
            def validate_arm(item: Any, path: str) -> None:
                arm = _projection_object(item, path, _RECALL_SELECTION_ARM_FIELDS)
                _require_projection_fields(arm, path, frozenset({"arm", "status", "candidate_count"}))
                for key in ("arm", "status"):
                    _projection_text(arm[key], f"{path}.{key}", max_chars=64)
                _projection_integer(arm["candidate_count"], f"{path}.candidate_count")
            _projection_array(trace["arms"], f"{field}.selection_decisions.arms", validate_arm)
        if "candidates" in trace:
            _projection_array(trace["candidates"], f"{field}.selection_decisions.candidates", _validate_recall_selection_decision)
        if "delivered_order" in trace:
            _projection_array(trace["delivered_order"], f"{field}.selection_decisions.delivered_order", lambda item, path: _projection_text(item, path, max_chars=256))
        if "replay_fingerprint_sha256" in trace:
            _projection_sha(trace["replay_fingerprint_sha256"], f"{field}.selection_decisions.replay_fingerprint_sha256")


def _validate_recall_wire_projections(response: Mapping[str, Any]) -> None:
    if "variants" in response:
        _nonnegative_int(response["variants"], "variants")
    validators = {
        "diagnostic": _validate_recall_diagnostic,
        "outcome": _validate_recall_outcome,
        "fused_trace": _validate_recall_fused,
        "freshness_summary": _validate_recall_freshness_summary,
        "freshness_gate": _validate_recall_freshness_gate,
        "evidence": _validate_recall_evidence,
        "declared_graph": _validate_recall_declared_graph,
    }
    for field, validator in validators.items():
        if field in response:
            validator(response[field], field)
    if "conflict_flags" in response:
        _projection_array(response["conflict_flags"], "conflict_flags", _validate_recall_conflict)
    for field in _RECALL_STRING_FIELDS:
        if field in response and not isinstance(response[field], str):
            raise ReplayValidationError(f"{field} must be a string when present")
    for field in _RECALL_BOOL_FIELDS:
        if field in response and not isinstance(response[field], bool):
            raise ReplayValidationError(f"{field} must be a boolean when present")
    profile = response.get("retrieval_profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise ReplayValidationError("retrieval_profile must be a non-empty string")


def _recall_wire_status(response: Mapping[str, Any], item_count: int, total: int, limit: int, offset: int) -> tuple[str, str | None]:
    if offset > total:
        return "unavailable", "offset_exceeds_total"
    if offset + item_count > total:
        return "unavailable", "page_exceeds_total"
    if item_count == 0 and total > 0 and offset != total:
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
            if total and offset != total:
                return "unavailable", "empty_outcome_positive_total"
            return "empty", None
        # `fresh` and wire-level `complete` use the envelope cardinality below.
    if item_count == 0 and total > 0 and offset != total:
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


def _strict_json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return (
            set(left) == set(right)
            and all(_strict_json_equal(left[key], right[key]) for key in left)
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _validate_recall_item_projection(item: Mapping[str, Any], index: int) -> None:
    body = item.get("body_json")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            body = None
    unknown = {
        key
        for key in set(item) - _RECALL_ITEM_FIELDS
        if not (isinstance(body, Mapping) and key in body and _strict_json_equal(item[key], body[key]))
    }
    if unknown:
        raise ReplayValidationError(f"recall item contains unknown field: {sorted(unknown)[0]}")
    if "why_served" in item:
        projection = item["why_served"]
        if not isinstance(projection, Mapping) or set(projection) - _WHY_SERVED_FIELDS:
            raise ReplayValidationError(f"recall item {index} has an unknown why_served projection")
        for field, value in projection.items():
            if field in {"support_count"}:
                _nonnegative_int(value, f"recall item {index}.why_served.{field}")
            elif field == "source_evidence_ids":
                if not isinstance(value, list) or len(value) > 4096:
                    raise ReplayValidationError(f"recall item {index}.why_served.{field} is malformed")
                for evidence_id in value:
                    _id(evidence_id, f"recall item {index}.why_served.{field}")
            elif (
                not isinstance(value, str)
                or len(value) > 256
                or (not value and field != "promoted_scope")
            ):
                raise ReplayValidationError(f"recall item {index}.why_served.{field} is malformed")
    if "wire_rank" in item:
        _positive_int(item["wire_rank"], f"recall item {index}.wire_rank")
    for field in ("created_at_unix_ms", "updated_at_unix_ms", "last_accessed_unix_ms", "as_of_unix_ms", "recorded_at_unix_ms", "valid_from_unix_ms", "valid_to_unix_ms"):
        if field in item and item[field] is not None:
            value = item[field]
            _nonnegative_int(value, f"recall item {index}.{field}")
            if value > _MAX_TIMESTAMP_UNIX_MS:
                raise ReplayValidationError(f"recall item {index}.{field} exceeds timestamp bound")


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
    The normalized mapping is transport-only and may retain bounded wire
    projections for in-process scoring; public artifacts must be built through
    ``build_snapshot``/``build_envelope``, which accept only canonical rows and
    hash or omit their raw fields.
    """
    try:
        limit = _positive_int(limit, "limit")
        offset = _nonnegative_int(offset, "offset")
        if limit > _MAX_RECALL_LIMIT:
            raise ReplayValidationError("limit exceeds protocol maximum")
        if offset > _MAX_RECALL_OFFSET:
            raise ReplayValidationError("offset exceeds protocol maximum")
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
        if not items and total > 0 and offset != total:
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
    except (KeyError, ReplayValidationError, TypeError, ValueError, OverflowError):
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


def _database_marker_material(*, nonce: str, binary_commit: str, device: int, inode: int) -> dict[str, Any]:
    return {
        "schema": _DATABASE_ATTESTATION_SCHEMA,
        "nonce": nonce,
        "binary_commit": binary_commit,
        "device": device,
        "inode": inode,
    }


def _database_attestation_material(
    *, nonce: str, binary_commit: str, device: int, inode: int,
    ctime_ns: int, size: int, user_version: int,
) -> dict[str, Any]:
    return {
        **_database_marker_material(
            nonce=nonce, binary_commit=binary_commit, device=device, inode=inode,
        ),
        "ctime_ns": ctime_ns,
        "size": size,
        "user_version": user_version,
    }


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
        connection.execute(f"PRAGMA application_id = {_DATABASE_APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()
        nonce = secrets.token_hex(32)
        marker_stat = database_path.stat()
        marker_material = _database_marker_material(
            nonce=nonce,
            binary_commit=binding["binary_commit"],
            device=marker_stat.st_dev,
            inode=marker_stat.st_ino,
        )
        marker_attestation = sha256_text(stable_json(marker_material))
        connection = sqlite3.connect(str(database_path))
        connection.execute(
            f"CREATE TABLE {_DATABASE_ATTESTATION_TABLE} ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "nonce TEXT NOT NULL, binary_commit TEXT NOT NULL, "
            "device INTEGER NOT NULL, inode INTEGER NOT NULL, "
            "attestation_sha256 TEXT NOT NULL)"
        )
        connection.execute(
            f"INSERT INTO {_DATABASE_ATTESTATION_TABLE} "
            "(id, nonce, binary_commit, device, inode, attestation_sha256) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (nonce, binding["binary_commit"], marker_stat.st_dev, marker_stat.st_ino, marker_attestation),
        )
        connection.commit()
        connection.close()
        database_stat = database_path.stat()
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()
    except (OSError, sqlite3.Error, TypeError, IndexError) as exc:
        raise ReplayValidationError("benchmark database could not be initialized") from exc
    database_identity = {
        "device": database_stat.st_dev,
        "inode": database_stat.st_ino,
        "ctime_ns": database_stat.st_ctime_ns,
        "size": database_stat.st_size,
    }
    attestation = sha256_text(
        stable_json(
            _database_attestation_material(
                nonce=nonce,
                binary_commit=binding["binary_commit"],
                device=database_identity["device"],
                inode=database_identity["inode"],
                ctime_ns=database_identity["ctime_ns"],
                size=database_identity["size"],
                user_version=user_version,
            )
        )
    )
    return {
        **binding,
        "database_fresh": True,
        "database_identity": database_identity,
        "database_id_sha256": sha256_text(stable_json(database_identity)),
        "database_attestation_sha256": attestation,
    }


def finalize_recall_preflight(preflight: Mapping[str, Any], *, db_path: str) -> dict[str, Any]:
    """Seal a fresh preflight after the benchmark has initialized and populated its DB."""
    _validate_preflight(preflight)
    database_path = Path(db_path).resolve()
    try:
        database_stat = database_path.stat()
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        row = connection.execute(
            f"SELECT nonce, binary_commit, device, inode, attestation_sha256 "
            f"FROM {_DATABASE_ATTESTATION_TABLE} WHERE id=1"
        ).fetchone()
        count = connection.execute(
            f"SELECT COUNT(*) FROM {_DATABASE_ATTESTATION_TABLE}"
        ).fetchone()[0]
        connection.close()
    except (OSError, sqlite3.Error, TypeError, IndexError) as exc:
        raise ReplayValidationError("benchmark database cannot be sealed") from exc
    if application_id != _DATABASE_APPLICATION_ID or user_version < 1 or count != 1 or row is None:
        raise ReplayValidationError("benchmark database activation marker is invalid")
    nonce, binary_commit, device, inode, stored_attestation = row
    if (
        not isinstance(nonce, str)
        or not re.fullmatch(r"[0-9a-f]{64}", nonce)
        or binary_commit != preflight["binary_commit"]
        or device != database_stat.st_dev
        or inode != database_stat.st_ino
    ):
        raise ReplayValidationError("benchmark database activation marker is not bound to runtime")
    marker_expected = sha256_text(
        stable_json(_database_marker_material(
            nonce=nonce, binary_commit=binary_commit, device=device, inode=inode,
        ))
    )
    if stored_attestation != marker_expected:
        raise ReplayValidationError("benchmark database activation marker digest mismatch")
    database_identity = {
        "device": database_stat.st_dev,
        "inode": database_stat.st_ino,
        "ctime_ns": database_stat.st_ctime_ns,
        "size": database_stat.st_size,
    }
    attestation = sha256_text(
        stable_json(
            _database_attestation_material(
                nonce=nonce,
                binary_commit=binary_commit,
                device=database_identity["device"],
                inode=database_identity["inode"],
                ctime_ns=database_identity["ctime_ns"],
                size=database_identity["size"],
                user_version=user_version,
            )
        )
    )
    return {
        **dict(preflight),
        "database_identity": database_identity,
        "database_id_sha256": sha256_text(stable_json(database_identity)),
        "database_attestation_sha256": attestation,
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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReplayValidationError(f"{field} must be finite")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ReplayValidationError(f"{field} must be finite") from exc
    if not math.isfinite(converted):
        raise ReplayValidationError(f"{field} must be finite")
    return converted


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
    if wire_rank != index + 1:
        raise ReplayValidationError(
            f"candidate {index}.wire_rank must equal its one-based wire position"
        )
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
    if "database_attestation_sha256" in preflight:
        _sha(preflight["database_attestation_sha256"], "preflight.database_attestation_sha256")
    if preflight["response_schema"] != RECALL_WIRE_SCHEMA_VERSION:
        raise ReplayValidationError("preflight response schema is unsupported")
    if preflight["response_schema_sha256"] != sha256_text(RECALL_WIRE_SCHEMA_VERSION):
        raise ReplayValidationError("preflight response schema digest mismatch")
    material = {key: preflight[key] for key in sorted(preflight)}
    return sha256_text(stable_json(material))


def _validate_runtime_database_attestation(
    preflight: Mapping[str, Any], database_path: Path, identity: Mapping[str, Any]
) -> None:
    attestation = preflight.get("database_attestation_sha256")
    if not isinstance(attestation, str):
        raise ReplayValidationError("preflight database attestation is missing")
    _sha(attestation, "preflight.database_attestation_sha256")
    try:
        database_stat = database_path.stat()
    except OSError as exc:
        raise ReplayValidationError("preflight database is not available at runtime") from exc
    current_identity = {
        "device": database_stat.st_dev,
        "inode": database_stat.st_ino,
        "ctime_ns": database_stat.st_ctime_ns,
        "size": database_stat.st_size,
    }
    if current_identity != dict(identity):
        raise ReplayValidationError("preflight database identity differs from current runtime")
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        row = connection.execute(
            f"SELECT nonce, binary_commit, device, inode, attestation_sha256 "
            f"FROM {_DATABASE_ATTESTATION_TABLE} WHERE id=1"
        ).fetchone()
        count = connection.execute(
            f"SELECT COUNT(*) FROM {_DATABASE_ATTESTATION_TABLE}"
        ).fetchone()[0]
        connection.close()
    except (OSError, sqlite3.Error, TypeError, IndexError) as exc:
        raise ReplayValidationError("preflight database attestation cannot be read") from exc
    if application_id != _DATABASE_APPLICATION_ID or user_version < 1:
        raise ReplayValidationError("preflight database activation marker is invalid")
    if count != 1 or row is None:
        raise ReplayValidationError("preflight database activation marker is missing")
    nonce, binary_commit, device, inode, stored_attestation = row
    if (
        not isinstance(nonce, str)
        or not re.fullmatch(r"[0-9a-f]{64}", nonce)
        or binary_commit != preflight["binary_commit"]
        or device != identity["device"]
        or inode != identity["inode"]
    ):
        raise ReplayValidationError("preflight database activation marker is not bound to runtime")
    marker_expected = sha256_text(
        stable_json(_database_marker_material(
            nonce=nonce, binary_commit=binary_commit, device=device, inode=inode,
        ))
    )
    if stored_attestation != marker_expected:
        raise ReplayValidationError("preflight database activation marker digest mismatch")
    expected = sha256_text(
        stable_json(
            _database_attestation_material(
                nonce=nonce,
                binary_commit=binary_commit,
                device=device,
                inode=inode,
                ctime_ns=current_identity["ctime_ns"],
                size=current_identity["size"],
                user_version=user_version,
            )
        )
    )
    if attestation != expected:
        raise ReplayValidationError("preflight database attestation digest mismatch")


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
    _validate_runtime_database_attestation(preflight, database_path, identity)
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
    if envelope["binding_mode"] == "runtime" and "database_attestation_sha256" not in envelope["preflight"]:
        raise ReplayValidationError("runtime envelope requires database attestation")
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
    "build_envelope", "build_snapshot", "normalize_recall_response", "recall_status_is_scoreable", "require_recall_items", "prepare_recall_preflight", "finalize_recall_preflight",
    "validate_recall_preflight",
    "replay_envelope", "sha256_text", "stable_json", "validate_envelope", "validate_snapshot",
]
