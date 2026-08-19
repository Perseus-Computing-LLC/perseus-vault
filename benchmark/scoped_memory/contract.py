"""Portable, synthetic scoped-memory capability contract (#1103).

This module is a benchmark/proof surface, not a second memory API.  The
``CapabilityBoundary`` owns trusted scope and authority, while ``Surface``
implementations only provide the existing read/write operations used by Vault.
The public projection contains hashes, bounded outcomes, and booleans only.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

CONTRACT_VERSION = "perseus-vault-scoped-memory-contract/v1"
OUTCOMES = frozenset({"allow", "deny", "scope_mismatch", "stale_conflict", "abstain", "unavailable"})
EXPECTED_CASES = (
    "surface-unavailable",
    "scope-injection",
    "cross-scope-search",
    "inspect-other-scope",
    "bounded-context",
    "empty-abstain",
    "read-only-write",
    "authorized-store",
    "correction-lineage",
    "stale-conflict",
    "supersession-lineage",
    "semantic-provider-unavailable",
)
_SCOPE_KEYS = frozenset({
    "user_id", "user", "workspace", "workspace_hash", "agent_id", "session_id", "scope", "trusted_scope"
})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ContractValidationError(ValueError):
    """A fixture, trusted scope, or public projection is malformed."""


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or ".." in value:
        raise ContractValidationError(f"{name} is unsafe")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ContractValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True)
class TrustedScope:
    """Host-authenticated scope; never constructed from model arguments."""

    user_id: str
    workspace_hash: str
    agent_id: str
    session_id: str

    def __post_init__(self) -> None:
        for name in ("user_id", "workspace_hash", "agent_id", "session_id"):
            _safe_id(name, getattr(self, name))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TrustedScope":
        if not isinstance(value, Mapping):
            raise ContractValidationError("trusted scope must be an object")
        required = ("user_id", "workspace_hash", "agent_id", "session_id")
        if any(key not in value for key in required):
            raise ContractValidationError("trusted scope is incomplete")
        return cls(*(value[key] for key in required))

    def as_mapping(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "workspace_hash": self.workspace_hash,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
        }

    def matches(self, other: "TrustedScope") -> bool:
        return self == other


@dataclass(frozen=True)
class TrustedAuthority:
    scope: TrustedScope
    allowed_operations: frozenset[str]
    authority_id: str = "authority:fixture-v1"

    def __post_init__(self) -> None:
        _safe_id("authority_id", self.authority_id)
        if not self.allowed_operations:
            raise ContractValidationError("authority must allow at least one operation")
        if any(not isinstance(item, str) or not _SAFE_ID.fullmatch(item) for item in self.allowed_operations):
            raise ContractValidationError("authority operation is unsafe")

    def allows(self, operation: str) -> bool:
        return operation in self.allowed_operations


@dataclass
class _Record:
    record_id: str
    category: str
    key: str
    scope: TrustedScope
    body: str
    source_digest: str
    source_ref: str
    status: str = "active"
    version: int = 1
    predecessor_id: str | None = None
    successor_id: str | None = None
    relation: str | None = None


class Surface(Protocol):
    available: bool

    def seed(self, fixture: Mapping[str, Any]) -> None: ...

    def search(self, scope: TrustedScope, query: str, limit: int, ranker: "RecordingRanker") -> dict[str, Any]: ...

    def context(self, scope: TrustedScope, query: str, budget_chars: int, limit: int, ranker: "RecordingRanker") -> dict[str, Any]: ...

    def inspect(self, scope: TrustedScope, record_id: str) -> dict[str, Any]: ...

    def store(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]: ...

    def correct(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]: ...

    def supersede(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]: ...

    def semantic_search(self, scope: TrustedScope, query: str) -> dict[str, Any]: ...


class RecordingRanker:
    """Deterministic test ranker; it records only already-authorized IDs."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def rank(self, candidates: Sequence[_Record]) -> list[_Record]:
        ordered = sorted(candidates, key=lambda candidate: candidate.record_id)
        self.seen.extend(candidate.record_id for candidate in ordered)
        return ordered


@dataclass
class ContractRun:
    contract_version: str
    outcomes: dict[str, dict[str, Any]]
    projection: dict[str, Any]
    ranker: RecordingRanker
    passed: bool
    failures: list[str] = field(default_factory=list)


class CapabilityBoundary:
    """Host boundary that injects scope and rejects model-authored scope."""

    def __init__(self, surface: Surface) -> None:
        self.surface = surface

    def invoke(
        self,
        operation: str,
        authority: TrustedAuthority,
        model_args: Mapping[str, Any] | None = None,
        *,
        ranker: RecordingRanker | None = None,
    ) -> dict[str, Any]:
        args = dict(model_args or {})
        if any(key in _SCOPE_KEYS for key in args):
            return _result("deny", "caller_scope_injection")
        if not authority.allows(operation):
            return _result("deny", "authority_missing")
        if not self.surface.available:
            return _result("unavailable", "surface_unavailable")
        try:
            if operation == "search":
                return self.surface.search(authority.scope, str(args.get("query", "")), int(args.get("limit", 5)), ranker or RecordingRanker())
            if operation == "context":
                return self.surface.context(authority.scope, str(args.get("query", "")), int(args.get("budget_chars", 256)), int(args.get("limit", 5)), ranker or RecordingRanker())
            if operation == "inspect":
                return self.surface.inspect(authority.scope, str(args.get("record_id", "")))
            if operation == "store":
                return self.surface.store(authority, args)
            if operation == "correct":
                return self.surface.correct(authority, args)
            if operation == "supersede":
                return self.surface.supersede(authority, args)
            if operation == "semantic_search":
                return self.surface.semantic_search(authority.scope, str(args.get("query", "")))
        except (TypeError, ValueError, ContractValidationError):
            return _result("deny", "malformed_arguments")
        return _result("deny", "unsupported_operation")


def _result(outcome: str, reason: str, **fields: Any) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise ContractValidationError(f"unknown outcome {outcome}")
    return {"outcome": outcome, "reason": reason, **fields}


def _query_matches(record: _Record, query: str) -> bool:
    terms = {part.lower() for part in query.split() if part.strip()}
    if not terms:
        return False
    haystack = " ".join((record.category, record.key, record.body)).lower()
    return any(term in haystack for term in terms)


class InProcessSurface:
    """Small in-process implementation of the existing Vault operation boundary."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self._records: dict[str, _Record] = {}

    def seed(self, fixture: Mapping[str, Any]) -> None:
        self._records.clear()
        for raw in fixture["records"]:
            record = _record_from_mapping(raw)
            self._records[record.record_id] = record

    def _active(self, scope: TrustedScope, query: str) -> list[_Record]:
        return [
            record
            for record in self._records.values()
            if record.status == "active" and record.scope.matches(scope) and _query_matches(record, query)
        ]

    def search(self, scope: TrustedScope, query: str, limit: int, ranker: RecordingRanker) -> dict[str, Any]:
        if not query.strip():
            return _result("abstain", "empty_query")
        if limit < 1 or limit > 5:
            return _result("deny", "bounded_limit")
        candidates = self._active(scope, query)
        ordered = ranker.rank(candidates)
        return _result("allow", "authorized_candidates", records=ordered[:limit], candidate_ids=[r.record_id for r in candidates])

    def context(self, scope: TrustedScope, query: str, budget_chars: int, limit: int, ranker: RecordingRanker) -> dict[str, Any]:
        if budget_chars < 1 or budget_chars > 512:
            return _result("deny", "bounded_context_budget")
        result = self.search(scope, query, limit, ranker)
        if result["outcome"] != "allow":
            return result
        records = []
        used = 0
        for record in result["records"]:
            cost = len(record.body)
            if used + cost > budget_chars:
                break
            records.append(record)
            used += cost
        return _result("allow", "bounded_context", records=records, total_chars=used, injected_chars=used, budget_chars=budget_chars, candidate_ids=result.get("candidate_ids", []))

    def inspect(self, scope: TrustedScope, record_id: str) -> dict[str, Any]:
        record = self._records.get(record_id)
        if record is None:
            return _result("abstain", "record_not_found")
        if not record.scope.matches(scope):
            return _result("scope_mismatch", "record_outside_trusted_scope")
        return _result("allow", "authorized_inspect", record=record)

    def store(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]:
        category = _safe_id("category", args.get("category"))
        key = _safe_id("key", args.get("key"))
        body = args.get("body")
        if not isinstance(body, str) or not body:
            return _result("deny", "malformed_arguments")
        record_id = _safe_id("record_id", args.get("record_id", f"stored:{category}:{key}"))
        if record_id in self._records:
            return _result("stale_conflict", "record_id_exists")
        record = _Record(record_id, category, key, authority.scope, body, sha256_text(body), f"fixture:{record_id}")
        self._records[record_id] = record
        return _result("allow", "authorized_store", record=record)

    def _current(self, record_id: str) -> _Record | None:
        record = self._records.get(record_id)
        return record if record and record.status == "active" else None

    def correct(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]:
        record_id = _safe_id("record_id", args.get("record_id"))
        old = self._current(record_id)
        if old is None:
            return _result("stale_conflict", "record_not_active")
        if not old.scope.matches(authority.scope):
            return _result("scope_mismatch", "record_outside_trusted_scope")
        if args.get("expected_version") != old.version:
            return _result("stale_conflict", "version_mismatch")
        body = args.get("body")
        if not isinstance(body, str) or not body:
            return _result("deny", "malformed_arguments")
        successor_id = f"{old.record_id}:corrected:v{old.version + 1}"
        successor = _Record(successor_id, old.category, f"{old.key}-corrected", old.scope, body, sha256_text(body), f"fixture:{successor_id}", predecessor_id=old.record_id, relation="corrects")
        old.status = "superseded"
        old.successor_id = successor_id
        self._records[successor_id] = successor
        return _result("allow", "authorized_correction", old=old, successor=successor)

    def supersede(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]:
        record_id = _safe_id("record_id", args.get("record_id"))
        old = self._current(record_id)
        if old is None:
            return _result("stale_conflict", "record_not_active")
        if not old.scope.matches(authority.scope):
            return _result("scope_mismatch", "record_outside_trusted_scope")
        successor_id = _safe_id("successor_id", args.get("successor_id"))
        body = args.get("body")
        if not isinstance(body, str) or not body:
            return _result("deny", "malformed_arguments")
        if successor_id in self._records:
            return _result("stale_conflict", "successor_exists")
        successor = _Record(successor_id, old.category, _safe_id("successor_key", args.get("successor_key", successor_id)), old.scope, body, sha256_text(body), f"fixture:{successor_id}", predecessor_id=old.record_id, relation="supersedes")
        old.status = "superseded"
        old.successor_id = successor_id
        self._records[successor_id] = successor
        return _result("allow", "authorized_supersession", old=old, successor=successor)

    def semantic_search(self, scope: TrustedScope, query: str) -> dict[str, Any]:
        return _result("unavailable", "semantic_provider_unavailable")


class McpSurface:
    """Adapter over the existing Python client and canonical MCP tools."""

    MCP_AGENT = "perseus-vault-client"

    def __init__(self, client: Any, *, available: bool = True) -> None:
        self.client = client
        self.available = available
        self._records: dict[str, _Record] = {}
        self._entity_ids: dict[str, str] = {}
        self._configured_workspaces: set[str] = set()

    def _call(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if hasattr(self.client, "call_tool"):
            result = self.client.call_tool(name, dict(args))
        else:
            result = self.client.call(name, dict(args))
        if not isinstance(result, dict):
            raise RuntimeError("MCP result is not an object")
        return result

    def _admission_client(self) -> Any:
        surface = self

        class Adapter:
            def call(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
                return surface._call(name, args)

        return Adapter()

    def _ensure_workspace(self, workspace: str) -> None:
        if workspace in self._configured_workspaces:
            return
        from benchmark.admission_fixture import configure

        configure(self._admission_client(), workspace=workspace, agent=self.MCP_AGENT)
        self._configured_workspaces.add(workspace)

    def _store_admitted(self, record: _Record) -> dict[str, Any]:
        from benchmark.admission_fixture import admitted_remember

        self._ensure_workspace(record.scope.workspace_hash)
        payload = {
            "scope": record.scope.as_mapping(),
            "memory": record.body,
            "source_digest": record.source_digest,
            "source_ref": record.source_ref,
        }
        result = admitted_remember(self._admission_client(), record.category, record.key, stable_json(payload), workspace=record.scope.workspace_hash, agent=self.MCP_AGENT)
        entity_id = result.get("id") if isinstance(result, dict) else None
        if isinstance(entity_id, str):
            self._entity_ids[record.record_id] = entity_id
        self._records[record.record_id] = record
        return result

    def seed(self, fixture: Mapping[str, Any]) -> None:
        self._records.clear()
        self._entity_ids.clear()
        for raw in fixture["records"]:
            self._store_admitted(_record_from_mapping(raw))

    def _normalize(self, item: Mapping[str, Any]) -> _Record | None:
        category = item.get("category")
        key = item.get("key")
        if not isinstance(category, str) or not isinstance(key, str):
            return None
        body = {}
        raw_body = item.get("body_json")
        if isinstance(raw_body, str):
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict):
                    body = parsed
            except json.JSONDecodeError:
                return None
        scope_value = body.get("scope")
        try:
            scope = TrustedScope.from_mapping(scope_value)
        except ContractValidationError:
            return None
        stable_id = next((rid for rid, record in self._records.items() if record.category == category and record.key == key and record.scope == scope), f"mcp:{category}:{key}")
        return _Record(stable_id, category, key, scope, str(body.get("memory", "")), str(body.get("source_digest", "")), str(body.get("source_ref", "")), str(item.get("status", "active")), 1)

    def search(self, scope: TrustedScope, query: str, limit: int, ranker: RecordingRanker) -> dict[str, Any]:
        if not query.strip():
            return _result("abstain", "empty_query")
        if limit < 1 or limit > 5:
            return _result("deny", "bounded_limit")
        response = self._call("perseus_vault_recall", {"query": query, "mode": "fts5", "limit": limit, "workspace_hash": scope.workspace_hash, "trust_weight": 0, "min_decay": 0})
        normalized = [self._normalize(item) for item in response.get("items", []) if isinstance(item, dict)]
        candidates = [record for record in normalized if record and record.status == "active" and record.scope.matches(scope) and _query_matches(record, query)]
        ordered = ranker.rank(candidates)
        return _result("allow", "authorized_candidates", records=ordered[:limit], candidate_ids=[record.record_id for record in candidates])

    def context(self, scope: TrustedScope, query: str, budget_chars: int, limit: int, ranker: RecordingRanker) -> dict[str, Any]:
        if budget_chars < 1 or budget_chars > 512:
            return _result("deny", "bounded_context_budget")
        response = self._call("perseus_vault_context", {"query": query, "mode": "on_demand", "limit": limit, "workspace_hash": scope.workspace_hash, "session_id": scope.session_id, "max_context_chars": budget_chars})
        actual = response.get("total_chars")
        injected = response.get("injected_chars", actual)
        if not isinstance(actual, int) or actual < 0 or not isinstance(injected, int) or injected < 0 or injected > budget_chars:
            return _result("deny", "context_budget_unbounded")
        return _result("allow", "bounded_context", total_chars=actual, injected_chars=injected, budget_chars=budget_chars, entities_injected=response.get("entities_injected", 0))

    def inspect(self, scope: TrustedScope, record_id: str) -> dict[str, Any]:
        record = self._records.get(record_id)
        if record is None:
            return _result("abstain", "record_not_found")
        response = self._call("perseus_vault_get_entity", {"id": self._entity_ids.get(record_id, record_id)})
        normalized = self._normalize(response)
        if normalized is None:
            return _result("abstain", "record_not_found")
        if not normalized.scope.matches(scope):
            return _result("scope_mismatch", "record_outside_trusted_scope")
        return _result("allow", "authorized_inspect", record=normalized)

    def store(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]:
        category = _safe_id("category", args.get("category"))
        key = _safe_id("key", args.get("key"))
        body = args.get("body")
        if not isinstance(body, str) or not body:
            return _result("deny", "malformed_arguments")
        record_id = _safe_id("record_id", args.get("record_id", f"stored:{category}:{key}"))
        if record_id in self._records:
            return _result("stale_conflict", "record_id_exists")
        record = _Record(record_id, category, key, authority.scope, body, sha256_text(body), f"fixture:{record_id}")
        self._store_admitted(record)
        return _result("allow", "authorized_store", record=record)

    def correct(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]:
        record_id = _safe_id("record_id", args.get("record_id"))
        old = self._records.get(record_id)
        if old is None or old.status != "active":
            return _result("stale_conflict", "record_not_active")
        if not old.scope.matches(authority.scope):
            return _result("scope_mismatch", "record_outside_trusted_scope")
        if args.get("expected_version") != old.version:
            return _result("stale_conflict", "version_mismatch")
        body = args.get("body")
        if not isinstance(body, str) or not body:
            return _result("deny", "malformed_arguments")
        correction = self._call("perseus_vault_correct", {
            "wrong_approach": old.body,
            "user_correction": body,
            "task_context": "scoped-memory-contract",
            "category": "contract-correction",
            "workspace_hash": old.scope.workspace_hash,
            "agent_id": old.scope.agent_id,
            "requesting_agent_id": self.MCP_AGENT,
            "session_id": old.scope.session_id,
        })
        if not isinstance(correction, dict):
            return _result("unavailable", "correction_surface_unavailable")
        successor_id = f"{old.record_id}:corrected:v{old.version + 1}"
        successor = _Record(successor_id, old.category, f"{old.key}-corrected", old.scope, body, sha256_text(body), f"fixture:{successor_id}", predecessor_id=old.record_id, relation="corrects")
        self._store_admitted(successor)
        old.status = "superseded"
        old.successor_id = successor_id
        return _result("allow", "authorized_correction", old=old, successor=successor, correction_receipt=bool(correction.get("entity_id")))

    def supersede(self, authority: TrustedAuthority, args: Mapping[str, Any]) -> dict[str, Any]:
        record_id = _safe_id("record_id", args.get("record_id"))
        old = self._records.get(record_id)
        if old is None or old.status != "active":
            return _result("stale_conflict", "record_not_active")
        if not old.scope.matches(authority.scope):
            return _result("scope_mismatch", "record_outside_trusted_scope")
        successor_id = _safe_id("successor_id", args.get("successor_id"))
        if successor_id in self._records:
            return _result("stale_conflict", "successor_exists")
        body = args.get("body")
        successor_key = _safe_id("successor_key", args.get("successor_key", successor_id))
        if not isinstance(body, str) or not body:
            return _result("deny", "malformed_arguments")
        successor = _Record(successor_id, old.category, successor_key, old.scope, body, sha256_text(body), f"fixture:{successor_id}", predecessor_id=old.record_id, relation="supersedes")
        self._store_admitted(successor)
        response = self._call("perseus_vault_supersede", {"from_category": old.category, "from_key": old.key, "to_category": successor.category, "to_key": successor.key, "reason": "scoped-memory-contract", "relationship": "supersedes"})
        if not isinstance(response, dict):
            return _result("unavailable", "supersession_surface_unavailable")
        old.status = "superseded"
        old.successor_id = successor_id
        return _result("allow", "authorized_supersession", old=old, successor=successor)

    def semantic_search(self, scope: TrustedScope, query: str) -> dict[str, Any]:
        return _result("unavailable", "semantic_provider_unavailable")


def _record_from_mapping(raw: Mapping[str, Any]) -> _Record:
    if not isinstance(raw, Mapping):
        raise ContractValidationError("record must be an object")
    record_id = _safe_id("record id", raw.get("id"))
    category = _safe_id("category", raw.get("category"))
    key = _safe_id("key", raw.get("key"))
    scope = TrustedScope.from_mapping(raw.get("scope"))
    body = raw.get("body")
    if not isinstance(body, str) or not body:
        raise ContractValidationError(f"record {record_id} body is missing")
    source_digest = _digest(raw.get("source_digest"), f"record {record_id} source_digest")
    if source_digest != sha256_text(body):
        raise ContractValidationError(f"record {record_id} source digest mismatch")
    source_ref = _safe_id("source_ref", raw.get("source_ref"))
    return _Record(record_id, category, key, scope, body, source_digest, source_ref)


def _canonical_fixture_without_digest(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(item) for key, item in value.items() if key != "fixture_sha256"}


def load_fixture(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    try:
        if isinstance(source, (str, Path)):
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        else:
            payload = deepcopy(dict(source))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ContractValidationError("fixture is missing or malformed") from exc
    if not isinstance(payload, dict) or payload.get("contract_version") != CONTRACT_VERSION:
        raise ContractValidationError("fixture contract version is unsupported")
    fixture_digest = _digest(payload.get("fixture_sha256"), "fixture_sha256")
    actual_fixture_digest = sha256_text(stable_json(_canonical_fixture_without_digest(payload)))
    if fixture_digest != actual_fixture_digest:
        raise ContractValidationError("fixture digest mismatch")
    if not isinstance(payload.get("records"), list) or not payload["records"]:
        raise ContractValidationError("fixture records must be a non-empty list")
    TrustedScope.from_mapping(payload.get("trusted_scope"))
    ids: set[str] = set()
    for raw in payload["records"]:
        record = _record_from_mapping(raw)
        if record.record_id in ids:
            raise ContractValidationError("fixture record IDs must be unique")
        ids.add(record.record_id)
    return payload


def _public_case(case_id: str, result: Mapping[str, Any], *, checks: Mapping[str, bool] | None = None, evidence: Mapping[str, Any] | None = None, blocking: bool = True) -> dict[str, Any]:
    outcome = result.get("outcome")
    reason = result.get("reason")
    if outcome not in OUTCOMES or not isinstance(reason, str) or not _SAFE_ID.fullmatch(reason):
        raise ContractValidationError(f"case {case_id} contains unsafe outcome data")
    row: dict[str, Any] = {
        "id": case_id,
        "outcome": outcome,
        "reason": reason,
        "blocking": bool(blocking),
        "checks": {str(key): bool(value) for key, value in sorted((checks or {}).items())},
        "evidence": {str(key): value for key, value in sorted((evidence or {}).items())},
    }
    return row


def _hash_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    return sha256_text(stable_json(list(cases)))


def _make_projection(outcomes: Mapping[str, Mapping[str, Any]], ranker: RecordingRanker, *, surface_available: bool, passed: bool) -> dict[str, Any]:
    cases = [
        {
            "id": case_id,
            "outcome": row["outcome"],
            "reason": row["reason"],
            "blocking": row["blocking"],
            "checks": row["checks"],
            "evidence": row["evidence"],
        }
        for case_id, row in sorted(outcomes.items())
    ]
    counts: dict[str, int] = {}
    for row in cases:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    base = {
        "contract_version": CONTRACT_VERSION,
        "status": "passed" if passed else "unavailable" if not surface_available else "failed",
        "case_count": len(cases),
        "outcome_counts": counts,
        "cases_digest": _hash_cases(cases),
        "ranker_seen_count": len(ranker.seen),
        "ranker_seen_digest": sha256_text(stable_json(ranker.seen)),
        "raw_inputs_captured": False,
    }
    receipt_sha256 = sha256_text(stable_json(base))
    projection_base = {**base, "receipt_sha256": receipt_sha256}
    projection_sha256 = sha256_text(stable_json(projection_base))
    return {**projection_base, "projection_sha256": projection_sha256}


def projection_digest(projection: Mapping[str, Any]) -> str:
    value = {key: deepcopy(item) for key, item in projection.items() if key != "projection_sha256"}
    return sha256_text(stable_json(value))


def verify_projection(projection: Mapping[str, Any]) -> None:
    if not isinstance(projection, Mapping):
        raise ContractValidationError("projection must be an object")
    for key in ("receipt_sha256", "projection_sha256"):
        _digest(projection.get(key), key)
    without_projection = {key: deepcopy(item) for key, item in projection.items() if key != "projection_sha256"}
    expected_projection = sha256_text(stable_json(without_projection))
    if expected_projection != projection["projection_sha256"]:
        raise ContractValidationError("projection digest mismatch")
    without_receipt = {key: deepcopy(item) for key, item in without_projection.items() if key != "receipt_sha256"}
    expected_receipt = sha256_text(stable_json(without_receipt))
    if expected_receipt != projection["receipt_sha256"]:
        raise ContractValidationError("receipt digest mismatch")
    if projection.get("raw_inputs_captured") is not False:
        raise ContractValidationError("raw inputs must not be captured")


def execute_contract(surface: Surface, *, fixture: Mapping[str, Any] | str | Path, ranker: RecordingRanker | None = None) -> ContractRun:
    payload = load_fixture(fixture)
    scope = TrustedScope.from_mapping(payload["trusted_scope"])
    ranker = ranker or RecordingRanker()
    authority = TrustedAuthority(scope, frozenset({"search", "context", "inspect", "store", "correct", "supersede", "semantic_search"}))
    surface.seed(payload)
    boundary = CapabilityBoundary(surface)
    outcomes: dict[str, dict[str, Any]] = {}

    availability = _result("allow", "surface_available") if surface.available else _result("unavailable", "surface_unavailable")
    outcomes["surface-unavailable"] = _public_case("surface-unavailable", availability, checks={"surface_available": surface.available}, evidence={"available": surface.available})
    if not surface.available:
        projection = _make_projection(outcomes, ranker, surface_available=False, passed=False)
        return ContractRun(CONTRACT_VERSION, outcomes, projection, ranker, False, ["surface-unavailable"])

    injected = boundary.invoke("search", authority, {"query": "synthetic anchor", "workspace_hash": "workspace-b"}, ranker=ranker)
    outcomes["scope-injection"] = _public_case("scope-injection", injected, checks={"rejected": injected["outcome"] == "deny"}, evidence={"scope_anchor": "trusted_host"})

    search = boundary.invoke("search", authority, {"query": "synthetic", "limit": 5}, ranker=ranker)
    candidate_ids = set(search.get("candidate_ids", []))
    allowed_ids = {raw["id"] for raw in payload["records"] if raw["scope"] == scope.as_mapping()}
    search_checks = {
        "authorized_candidates_only": candidate_ids.issubset(allowed_ids),
        "cross_scope_hidden": "anchor-b" not in candidate_ids and "anchor-session" not in candidate_ids,
        "ranker_after_scope_filter": candidate_ids == set(ranker.seen),
    }
    outcomes["cross-scope-search"] = _public_case("cross-scope-search", search, checks=search_checks, evidence={"count": len(candidate_ids), "scope_anchor": "trusted_host"})

    other = boundary.invoke("inspect", authority, {"record_id": "anchor-b"})
    outcomes["inspect-other-scope"] = _public_case("inspect-other-scope", other, checks={"mismatch_rejected": other["outcome"] == "scope_mismatch"}, evidence={"scope_anchor": "trusted_host"})

    context = boundary.invoke("context", authority, {"query": "synthetic", "budget_chars": 512, "limit": 3}, ranker=ranker)
    context_size = context.get("injected_chars", context.get("total_chars", 0))
    outcomes["bounded-context"] = _public_case("bounded-context", context, checks={"context_allowed": context["outcome"] == "allow", "bounded": context_size <= context.get("budget_chars", 0)}, evidence={"budget_chars": context.get("budget_chars", 0), "injected_chars": context_size})

    empty = boundary.invoke("search", authority, {"query": ""}, ranker=ranker)
    outcomes["empty-abstain"] = _public_case("empty-abstain", empty, checks={"explicit_abstain": empty["outcome"] == "abstain"}, evidence={"empty_abstained": empty["outcome"] == "abstain"})

    read_only = TrustedAuthority(scope, frozenset({"search", "context", "inspect"}))
    denied_write = boundary.invoke("store", read_only, {"category": "contract", "key": "read-only", "body": "synthetic"})
    outcomes["read-only-write"] = _public_case("read-only-write", denied_write, checks={"write_denied": denied_write["outcome"] == "deny"}, evidence={"status": denied_write["reason"]})

    stored = boundary.invoke("store", authority, {"category": "contract", "key": "authorized", "record_id": "authorized-store", "body": "synthetic authorized store"})
    outcomes["authorized-store"] = _public_case("authorized-store", stored, checks={"write_allowed": stored["outcome"] == "allow"}, evidence={"receipt_present": stored["outcome"] == "allow"})

    corrected = boundary.invoke("correct", authority, {"record_id": "correctable", "expected_version": 1, "body": "synthetic corrected value"})
    correction_checks = {
        "old_record_retained": corrected.get("old") is not None,
        "successor_explicit": corrected.get("successor") is not None and bool(corrected.get("successor").successor_id is None if isinstance(corrected.get("successor"), _Record) else True),
        "successor_active": isinstance(corrected.get("successor"), _Record) and corrected["successor"].status == "active",
    }
    outcomes["correction-lineage"] = _public_case("correction-lineage", corrected, checks=correction_checks, evidence={"receipt_present": corrected.get("correction_receipt", True), "current_key_present": correction_checks["successor_active"], "superseded_evidence_present": correction_checks["old_record_retained"]})

    stale = boundary.invoke("correct", authority, {"record_id": "correctable", "expected_version": 1, "body": "synthetic stale correction"})
    outcomes["stale-conflict"] = _public_case("stale-conflict", stale, checks={"stale_rejected": stale["outcome"] == "stale_conflict"}, evidence={"status": stale["reason"]})

    superseded = boundary.invoke("supersede", authority, {"record_id": "supersede-old", "successor_id": "supersede-new", "successor_key": "supersede-new", "body": "synthetic successor value"})
    old = superseded.get("old")
    successor = superseded.get("successor")
    supersede_checks = {
        "old_record_retained": isinstance(old, _Record),
        "old_is_superseded": isinstance(old, _Record) and old.status == "superseded",
        "successor_active": isinstance(successor, _Record) and successor.status == "active",
    }
    outcomes["supersession-lineage"] = _public_case("supersession-lineage", superseded, checks=supersede_checks, evidence={"current_key_present": supersede_checks["successor_active"], "superseded_evidence_present": supersede_checks["old_is_superseded"]})

    semantic = boundary.invoke("semantic_search", authority, {"query": "synthetic"})
    outcomes["semantic-provider-unavailable"] = _public_case("semantic-provider-unavailable", semantic, checks={"explicit_unavailable": semantic["outcome"] == "unavailable"}, evidence={"available": False, "status": "unavailable"}, blocking=False)

    failures = [case_id for case_id, row in outcomes.items() if row["blocking"] and (not row["checks"] or not all(row["checks"].values()))]
    passed = not failures
    projection = _make_projection(outcomes, ranker, surface_available=True, passed=passed)
    verify_projection(projection)
    return ContractRun(CONTRACT_VERSION, outcomes, projection, ranker, passed, failures)


__all__ = [
    "CONTRACT_VERSION", "EXPECTED_CASES", "ContractRun", "ContractValidationError", "InProcessSurface",
    "McpSurface", "RecordingRanker", "TrustedAuthority", "TrustedScope", "execute_contract",
    "load_fixture", "projection_digest", "stable_json", "verify_projection",
]
