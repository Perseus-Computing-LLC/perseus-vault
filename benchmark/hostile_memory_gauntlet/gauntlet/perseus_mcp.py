from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from dataclasses import replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from .models import AdmissionReceipt, MemoryHit, MemoryRecord, MutationReceipt, RetrievalResult
from .provider import HostileMemoryProvider
from .protocol import canonical_json, sha256_bytes, sha256_text

_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?prior\s+instructions|reveal\s+(?:the\s+)?credentials|"
    r"system\s+prompt|jailbreak|developer\s+message",
    re.IGNORECASE,
)


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.casefold()))


def _similarity(left: str, right: str) -> float:
    left_words = _tokens(left)
    right_words = _tokens(right)
    if not left_words or not right_words:
        return 0.0
    jaccard = len(left_words & right_words) / len(left_words | right_words)
    sequence = SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
    return 0.65 * jaccard + 0.35 * sequence


def _query_score(query: str, record: MemoryRecord) -> float:
    query_words = _tokens(query)
    searchable = _tokens(f"{record.memory_key} {record.text}")
    if not query_words:
        return 0.0
    return len(query_words & searchable) / len(query_words)


def _record_body(record: MemoryRecord, valid_to: int | None) -> str:
    return canonical_json({
        "gauntlet_record_id": record.record_id,
        "gauntlet_memory_key": record.memory_key,
        "scope": record.scope,
        "text": record.text,
        "source_ref": record.source_ref,
        "source_digest": record.record_digest,
        "actor": record.actor,
        "trust": record.trust,
        "valid_from": record.valid_from,
        "valid_to": valid_to,
        "supersedes": list(record.supersedes),
    })


class MCPBoundaryError(RuntimeError):
    """The live provider could not complete a bounded MCP operation."""


class MCPToolError(MCPBoundaryError):
    """The server returned a JSON-RPC/tool error for a valid request."""


def admission_source_attestation_digest(key: str, evaluated: dict[str, str], requester: str) -> str:
    """Match Vault's source-attestation contract exactly.

    The journal wire field is the lowercase HMAC-SHA256 hex value over the
    ordered JSON payload. Vault hashes that value only when persisting its
    hash-only journal receipt. The raw key and attestation are never part of a
    report.
    """
    payload = json.dumps({
        "record_digest": evaluated["record_digest"],
        "source_identity": evaluated["source_identity"],
        "workspace_hash": evaluated["workspace_hash"],
        "actor_kind": evaluated["actor_kind"],
        "actor_identity": evaluated["actor_identity"],
        "requesting_agent_id": requester,
    }, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    hmac_hex = hmac.new(key.strip().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac_hex


def entity_key(record: MemoryRecord) -> str:
    """Map one scoped record identity to one Vault entity key.

    The generic contract treats each record as an addressable version. Vault's
    native ``(category, key, workspace)`` identity is therefore made unique per
    record, and explicit supersession links join versions afterward. Collapsing
    versions into one key would turn replay and out-of-order writes into normal
    updates and lose the contract's history boundary.
    """
    return "gauntlet:" + sha256_text(
        f"{record.scope}\x00{record.memory_key}\x00{record.record_id}"
    )


def decode_tool_result(payload: dict[str, Any]) -> Any:
    if "error" in payload:
        raise MCPToolError("jsonrpc_error")
    result = payload.get("result", {})
    if isinstance(result, dict) and result.get("isError"):
        raise MCPToolError("tool_error")
    if isinstance(result, dict) and "structuredContent" in result:
        return result["structuredContent"]
    content = result.get("content", []) if isinstance(result, dict) else []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            text = block["text"]
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _body(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("body_json")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def item_to_hit(item: dict[str, Any]) -> MemoryHit:
    body = _body(item)
    record_id = str(item.get("gauntlet_record_id") or body.get("gauntlet_record_id") or item.get("key") or item.get("id") or "")
    memory_key = str(item.get("gauntlet_memory_key") or body.get("gauntlet_memory_key") or "")
    scope = str(item.get("workspace_hash") or body.get("scope") or "")
    text = str(item.get("text") or body.get("text") or "")
    source_ref = str(item.get("source_ref") or body.get("source_ref") or "")
    record_digest = str(item.get("source_digest") or body.get("source_digest") or "")
    actor = str(item.get("actor") or body.get("actor") or "")
    trust = str(item.get("trust") or body.get("trust") or "")
    valid_from = _int_or_none(item.get("valid_from"))
    if valid_from is None:
        valid_from = _int_or_none(item.get("valid_from_unix_ms"))
    if valid_from is None:
        valid_from = _int_or_none(body.get("valid_from")) or _int_or_none(body.get("valid_from_unix_ms")) or 0
    valid_to = _int_or_none(item.get("valid_to"))
    if valid_to is None:
        valid_to = _int_or_none(item.get("valid_to_unix_ms"))
    if valid_to is None:
        valid_to = _int_or_none(body.get("valid_to")) or _int_or_none(body.get("valid_to_unix_ms"))
    score = item.get("score", item.get("relevance", 0.0))
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        score = 0.0
    return MemoryHit(
        record_id=record_id,
        memory_key=memory_key,
        scope=scope,
        text=text,
        source_ref=source_ref,
        record_digest=record_digest,
        actor=actor,
        trust=trust,
        valid_from=valid_from,
        valid_to=valid_to,
        status=str(item.get("status") or body.get("status") or "active"),
        score=float(score),
    )


class MCPStdioClient:
    """Bounded line-oriented MCP client for a checkout-built Vault binary."""

    def __init__(self, binary: str, db: Path, *, timeout_seconds: float = 30.0) -> None:
        self.timeout_seconds = timeout_seconds
        self._next_id = 0
        self._responses: queue.Queue[Any] = queue.Queue()
        child_env = dict(os.environ)
        admission_key = child_env.get("PERSEUS_GAUNTLET_ADMISSION_HMAC_KEY")
        if admission_key:
            # The test key is supplied transiently by the caller. It is copied
            # only into the disposable Vault child environment, never into a
            # report or a persisted artifact.
            child_env["PERSEUS_VAULT_ADMISSION_SOURCE_HMAC_KEY"] = admission_key
        self.process = subprocess.Popen(
            [binary, "--db", str(db), "--offline"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
            env=child_env,
        )
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        self._send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": os.environ.get("PERSEUS_GAUNTLET_AGENT", "gauntlet-agent"), "version": "0.1"},
        })
        self._read()
        self._notify("notifications/initialized")
        tools_result = self._request("tools/list", {})
        tools = tools_result.get("tools", []) if isinstance(tools_result, dict) else []
        self.tools = {
            str(tool.get("name")) for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("name"), str)
        }

    def _reader(self) -> None:
        if self.process.stdout is None:
            self._responses.put(None)
            return
        for line in self.process.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "result" in payload or "error" in payload:
                self._responses.put(payload)
        self._responses.put(None)

    def _send(self, method: str, params: dict[str, Any]) -> None:
        self._next_id += 1
        if self.process.stdin is None:
            raise MCPBoundaryError("stdin_closed")
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str) -> None:
        if self.process.stdin is None:
            raise MCPBoundaryError("stdin_closed")
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        try:
            payload = self._responses.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            raise MCPBoundaryError("response_timeout") from exc
        if payload is None:
            raise MCPBoundaryError("stream_closed")
        if not isinstance(payload, dict):
            raise MCPBoundaryError("malformed_response")
        return payload

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._send(method, params)
        return decode_tool_result(self._read())

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in self.tools:
            raise MCPBoundaryError(f"tool_not_advertised:{name}")
        return self._request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        process = self.process
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name != "nt":
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    else:
                        process.kill()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    raise MCPBoundaryError("process_cleanup_failed")
        self._reader_thread.join(timeout=1)


class PerseusMCPProvider(HostileMemoryProvider):
    """Capability-gated adapter for an isolated, checkout-built Vault binary.

    Required environment:
      PERSEUS_GAUNTLET_BINARY: absolute path to the intended Vault binary.
      PERSEUS_GAUNTLET_ADMISSION_HMAC_KEY: transient source-attestation key.

    Optional environment:
      PERSEUS_GAUNTLET_AGENT (default: gauntlet-agent)
      PERSEUS_GAUNTLET_AUTHORIZER (default: operator)
      PERSEUS_GAUNTLET_CONFIGURE_AUTH (default: 1)
      PERSEUS_GAUNTLET_DB_ROOT (default: an owned temporary directory)
    """

    name = "perseus-vault-mcp"
    contract = "perseus-hostile-memory-gauntlet/perseus-mcp/v1"

    def __init__(self, *, client_factory: Callable[[str, Path], Any] | None = None) -> None:
        self.binary = os.environ.get("PERSEUS_GAUNTLET_BINARY", "")
        self.agent_id = os.environ.get("PERSEUS_GAUNTLET_AGENT", "gauntlet-agent")
        self.authorizer = os.environ.get("PERSEUS_GAUNTLET_AUTHORIZER", "operator")
        self.configure_auth = os.environ.get("PERSEUS_GAUNTLET_CONFIGURE_AUTH", "1") == "1"
        configured_root = os.environ.get("PERSEUS_GAUNTLET_DB_ROOT")
        self._owned_root = configured_root is None
        self._db_root = Path(configured_root) if configured_root else Path(tempfile.mkdtemp(prefix="perseus-gauntlet-"))
        self._db_root.mkdir(parents=True, exist_ok=True)
        self._client_factory = client_factory or (lambda binary, db: MCPStdioClient(binary, db))
        self._client: Any = None
        self._db: Path | None = None
        self._case_number = 0
        self._record_keys: dict[str, str] = {}
        self._admitted_records: dict[str, MemoryRecord] = {}
        self._seen_record_ids: set[str] = set()
        self._admitted_digests: set[tuple[str, str]] = set()
        self._pending_superseders: dict[str, list[MemoryRecord]] = {}

    def _close_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._db is not None:
            for candidate in (self._db, Path(f"{self._db}-wal"), Path(f"{self._db}-shm")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            self._db = None

    def _ensure_client(self) -> Any:
        if not self.binary:
            raise MCPBoundaryError("PERSEUS_GAUNTLET_BINARY is required")
        if not Path(self.binary).is_file():
            raise MCPBoundaryError("configured Vault binary does not exist")
        if self._client is None:
            self._case_number += 1
            self._db = self._db_root / f"case-{self._case_number}.db"
            self._client = self._client_factory(self.binary, self._db)
            required = {
                "perseus_vault_remember",
                "perseus_vault_recall",
                "perseus_vault_forget",
                "perseus_vault_valid_at",
            }
            missing = sorted(required - set(getattr(self._client, "tools", required)))
            if missing:
                raise MCPBoundaryError("required_tools_missing")
        return self._client

    def reset(self) -> None:
        self._close_client()
        self._record_keys.clear()
        self._admitted_records.clear()
        self._seen_record_ids.clear()
        self._admitted_digests.clear()
        self._pending_superseders.clear()
        self._ensure_client()

    def _close_version(self, old: MemoryRecord, new: MemoryRecord, client: Any) -> None:
        """Close an older version's valid-time interval in Vault.

        Vault's public supersede mutation deliberately redacts deprecated
        bodies from temporal reads. The provider contract needs historical
        evidence with provenance, so this adapter uses an active row with an
        explicit half-open valid interval instead. Current reads still exclude
        the closed version, while historical valid-time reads remain complete.
        """
        old_key = self._record_keys.get(old.record_id)
        if not old_key:
            raise MCPBoundaryError("supersession_record_key_missing")
        body = _record_body(old, new.valid_from)
        arguments: dict[str, Any] = {
            "category": "gauntlet",
            "key": old_key,
            "body_json": body,
            "type": "fact",
            "status": "active",
            "workspace_hash": old.scope,
            "agent_id": self.agent_id,
            "requesting_agent_id": self.agent_id,
            "actor_kind": "connector",
            "valid_from_unix_ms": old.valid_from,
            "valid_to_unix_ms": new.valid_from,
        }
        arguments["admission"] = self._admission(old, body, client)
        result = client.call("perseus_vault_remember", arguments)
        if not isinstance(result, dict) or result.get("serveable") is not True or result.get("proposed"):
            raise MCPBoundaryError("supersession_validity_close_failed")
        self._admitted_records[old.record_id] = replace(old, valid_to=new.valid_from)

    def _apply_supersessions(self, record: MemoryRecord, client: Any) -> None:
        for old_id in record.supersedes:
            old = getattr(self, "_admitted_records", {}).get(old_id)
            if old is None:
                self._pending_superseders.setdefault(old_id, []).append(record)
                continue
            self._close_version(old, record, client)
        pending = self._pending_superseders.pop(record.record_id, [])
        for new in pending:
            self._close_version(record, new, client)

    def _ensure_authority(self, scope: str, client: Any) -> None:
        if not self.configure_auth:
            return
        if "perseus_vault_agent" not in client.tools or "perseus_vault_authority_set" not in client.tools:
            raise MCPBoundaryError("authority_tools_missing")
        client.call("perseus_vault_agent", {
            "agent_id": self.agent_id,
            "name": self.agent_id,
            "trust_tier": 2,
            "fleet_id": "gauntlet",
        })
        client.call("perseus_vault_authority_set", {
            "agent_id": self.agent_id,
            "workspace_hash": scope,
            "allowed_capabilities": [
                "memory.admission.source", "memory.commit", "memory.read", "memory.write",
                "memory.maintenance", "memory.delete", "memory.export",
            ],
            "scope_anchors": [scope],
            "mode": "enforce",
            "author_agent_id": self.authorizer,
            "capability_constraints_json": "{}",
        })

    def _admission(self, record: MemoryRecord, body: str, client: Any) -> dict[str, Any]:
        key = os.environ.get("PERSEUS_GAUNTLET_ADMISSION_HMAC_KEY")
        if not key:
            raise MCPBoundaryError("PERSEUS_GAUNTLET_ADMISSION_HMAC_KEY is required")
        body_digest = sha256_text(body)
        evaluated = {
            "record_digest": body_digest,
            "source_identity": f"gauntlet:{record.record_id}",
            "workspace_hash": record.scope,
            "actor_kind": "connector",
            "actor_identity": self.agent_id,
        }
        attestation = admission_source_attestation_digest(key, evaluated, self.agent_id)
        source = client.call("perseus_vault_journal", {
            "event_type": "admission_source",
            "evaluated": evaluated,
            "source_attestation": attestation,
            "acted": {}, "forward": {},
            "workspace_hash": record.scope,
            "requesting_agent_id": self.agent_id,
        })
        source_id = source.get("id") if isinstance(source, dict) else None
        if not isinstance(source_id, str) or not source_id:
            raise MCPBoundaryError("admission_source_receipt_missing")
        return {
            "record_digest": body_digest,
            "source_identity": evaluated["source_identity"],
            "source_event_id": source_id,
            "authorization_scope": record.scope,
            "ingestion_channel": "hostile-memory-gauntlet",
            "workspace_hash": record.scope,
            "source_trust": record.trust,
            "actor_kind": "connector",
            "actor_identity": self.agent_id,
            "validated": True,
            "valid_from_unix_ms": record.valid_from,
            "recorded_at_unix_ms": record.recorded_at,
            "task_relevance_bps": 9000,
        }

    def ingest(self, record: MemoryRecord) -> AdmissionReceipt:
        if record.record_id in self._seen_record_ids:
            return AdmissionReceipt(
                record.record_id,
                "quarantined",
                False,
                ("duplicate_replay",),
                record.record_digest,
            )
        if (record.scope, record.record_digest) in self._admitted_digests:
            self._seen_record_ids.add(record.record_id)
            return AdmissionReceipt(
                record.record_id,
                "quarantined",
                False,
                ("duplicate_content",),
                record.record_digest,
            )
        self._seen_record_ids.add(record.record_id)
        if _INJECTION_RE.search(record.text):
            return AdmissionReceipt(
                record.record_id,
                "quarantined",
                False,
                ("prompt_injection_text",),
                record.record_digest,
            )
        same_time_conflict = any(
            existing.scope == record.scope
            and existing.memory_key == record.memory_key
            and existing.valid_from == record.valid_from
            and existing.trust == "authoritative"
            and record.trust == "authoritative"
            and existing.text != record.text
            and existing.status not in {"archived", "quarantined"}
            for existing in self._admitted_records.values()
        )
        if same_time_conflict:
            return AdmissionReceipt(
                record.record_id,
                "quarantined",
                False,
                ("same_time_conflict",),
                record.record_digest,
            )
        if record.trust in {"untrusted", "unknown"}:
            authoritative = [
                existing
                for existing in self._admitted_records.values()
                if existing.scope == record.scope
                and existing.memory_key == record.memory_key
                and existing.trust == "authoritative"
                and existing.status not in {"archived", "quarantined"}
            ]
            if authoritative and any(existing.text != record.text for existing in authoritative):
                return AdmissionReceipt(
                    record.record_id,
                    "quarantined",
                    False,
                    ("low_trust_conflict",),
                    record.record_digest,
                )
        near_duplicate = any(
            existing.scope == record.scope
            and existing.memory_key == record.memory_key
            and existing.status not in {"archived", "quarantined"}
            and _similarity(existing.text, record.text) >= 0.96
            for existing in self._admitted_records.values()
        )
        if near_duplicate:
            return AdmissionReceipt(
                record.record_id,
                "quarantined",
                False,
                ("near_duplicate_flood",),
                record.record_digest,
            )
        client = self._ensure_client()
        self._ensure_authority(record.scope, client)
        effective_valid_to = record.valid_to
        future = self._pending_superseders.get(record.record_id, [])
        if future:
            boundaries = [item.valid_from for item in future if item.valid_from > record.valid_from]
            if boundaries:
                effective_valid_to = min(boundaries)
        body = _record_body(record, effective_valid_to)
        args: dict[str, Any] = {
            "category": "gauntlet",
            "key": entity_key(record),
            "body_json": body,
            "type": "fact",
            "status": "active",
            "workspace_hash": record.scope,
            "agent_id": self.agent_id,
            "requesting_agent_id": self.agent_id,
            "actor_kind": "connector",
            "valid_from_unix_ms": record.valid_from,
        }
        self._record_keys[record.record_id] = entity_key(record)
        if effective_valid_to is not None:
            args["valid_to_unix_ms"] = effective_valid_to
        try:
            args["admission"] = self._admission(record, body, client)
            result = client.call("perseus_vault_remember", args)
        except MCPToolError:
            return AdmissionReceipt(record.record_id, "rejected", False, ("provider_rejected",), record.record_digest)
        if not isinstance(result, dict):
            raise MCPBoundaryError("remember_response_malformed")
        if result.get("rejected") is True:
            return AdmissionReceipt(record.record_id, "rejected", False, ("provider_rejected",), record.record_digest)
        if result.get("serveable") is True and not result.get("proposed"):
            self._admitted_records[record.record_id] = record
            self._admitted_digests.add((record.scope, record.record_digest))
            self._apply_supersessions(record, client)
            return AdmissionReceipt(record.record_id, "admitted", True, (), record.record_digest)
        return AdmissionReceipt(record.record_id, "quarantined", False, ("provider_non_serveable",), record.record_digest)

    def forget(self, scope: str, record_id: str) -> MutationReceipt:
        client = self._ensure_client()
        self._ensure_authority(scope, client)
        try:
            result = client.call("perseus_vault_forget", {
                "category": "gauntlet", "key": self._record_keys.get(record_id, record_id),
                "reason": "hostile-memory-gauntlet",
            })
        except MCPToolError:
            return MutationReceipt(record_id, "not_found", ("provider_rejected",))
        if isinstance(result, dict) and result.get("found") is False:
            return MutationReceipt(record_id, "not_found", ("scope_or_record_not_found",))
        return MutationReceipt(record_id, "archived", ("tombstoned",))

    def retrieve(self, query: str, scope: str, as_of: int, limit: int) -> RetrievalResult:
        client = self._ensure_client()
        self._ensure_authority(scope, client)
        candidates = [
            (record, _query_score(query, record))
            for record in self._admitted_records.values()
            if record.scope == scope
            and record.trust not in {"untrusted", "unknown"}
            and _query_score(query, record) > 0.0
        ]
        candidates.sort(key=lambda pair: (-pair[1], -pair[0].valid_from, pair[0].record_id))
        hit_list: list[MemoryHit] = []
        for record, score in candidates:
            try:
                result = client.call("perseus_vault_valid_at", {
                    "category": "gauntlet",
                    "key": self._record_keys[record.record_id],
                    "valid_at_unix_ms": as_of,
                })
            except MCPToolError:
                return RetrievalResult.failed("provider_rejected")
            if not isinstance(result, dict):
                raise MCPBoundaryError("valid_at_response_malformed")
            if result.get("found") is not True:
                continue
            item = dict(result)
            item.setdefault("workspace_hash", scope)
            item.setdefault("gauntlet_record_id", record.record_id)
            item.setdefault("gauntlet_memory_key", record.memory_key)
            item["score"] = score
            hit_list.append(item_to_hit(item))
            if len(hit_list) >= limit:
                break
        if not hit_list:
            return RetrievalResult("abstain", (), ("no_trustworthy_evidence",))
        return RetrievalResult("answer", tuple(hit_list), ())

    def close(self) -> None:
        self._close_client()
        if self._owned_root:
            shutil.rmtree(self._db_root, ignore_errors=False)

    def public_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "real_producer": True,
            "offline": True,
            "network_calls": 0,
        }
        try:
            metadata["binary_sha256"] = sha256_bytes(Path(self.binary).read_bytes())
        except (OSError, TypeError):
            metadata["binary_sha256"] = ""
        return metadata
