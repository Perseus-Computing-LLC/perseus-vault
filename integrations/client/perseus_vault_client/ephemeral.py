"""Ephemeral, admitted integration fixture for the public MCP client.

The fixture deliberately owns its database and generates a fresh source-event
HMAC key for every process. It is intended for CI and local integration tests,
not for opening an existing Vault database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import VaultClient, VaultError


class EphemeralAdmissionFixture:
    """Run a real Vault against an owned temporary database.

    The fixture configures the minimum authority needed for an authoritative
    source event, commit, and read, then performs admitted writes through the
    normal public MCP tools. It accepts no database path or caller-supplied
    key, which prevents accidental use against a normal production store.
    """

    WORKSPACE = "perseus-ephemeral-fixture"
    AGENT = "perseus-ephemeral-fixture"
    _OPERATOR = "perseus-ephemeral-fixture-operator"

    def __init__(self, binary: Optional[str] = None, *, timeout: float = 30.0):
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="perseus-vault-ephemeral-"
        )
        self.db_path = str(Path(self._temporary_directory.name) / "vault.db")
        self._source_hmac_key = secrets.token_hex(32)
        self._client = VaultClient(
            binary=binary,
            db_path=self.db_path,
            timeout=timeout,
            env={
                "PERSEUS_VAULT_ADMISSION_SOURCE_HMAC_KEY": self._source_hmac_key,
            },
            client_info_name=self.AGENT,
        )
        self._closed = False

    @property
    def client(self) -> VaultClient:
        """The underlying public MCP client for additional test calls."""
        return self._client

    def __enter__(self) -> "EphemeralAdmissionFixture":
        try:
            self._client.__enter__()
            self._configure_authority()
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop Vault and remove the fixture directory; safe to call twice."""
        if self._closed:
            return
        self._client.close()
        self._temporary_directory.cleanup()
        self._closed = True

    def _configure_authority(self) -> None:
        self._client.call_tool(
            "perseus_vault_agent",
            {
                "agent_id": self.AGENT,
                "name": self.AGENT,
                "trust_tier": 2,
                "fleet_id": "ephemeral-fixture",
            },
        )
        self._client.call_tool(
            "perseus_vault_authority_set",
            {
                "agent_id": self.AGENT,
                "workspace_hash": self.WORKSPACE,
                "allowed_capabilities": [
                    "memory.admission.source",
                    "memory.commit",
                    "memory.read",
                ],
                "scope_anchors": [self.WORKSPACE],
                "mode": "enforce",
                "author_agent_id": self._OPERATOR,
                "capability_constraints_json": "{}",
            },
        )

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def remember(self, category: str, key: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """Write one authoritative, serveable synthetic record through MCP."""
        body_json = self._stable_json(body)
        record_digest = hashlib.sha256(body_json.encode("utf-8")).hexdigest()
        source_identity = f"{category}:{key}"
        evaluated = {
            "record_digest": record_digest,
            "source_identity": source_identity,
            "workspace_hash": self.WORKSPACE,
            "actor_kind": "connector",
            "actor_identity": self.AGENT,
        }
        attestation_payload = self._stable_json(
            {**evaluated, "requesting_agent_id": self.AGENT}
        )
        source_attestation = hmac.new(
            self._source_hmac_key.encode("utf-8"),
            attestation_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        source = self._client.call_tool(
            "perseus_vault_journal",
            {
                "event_type": "admission_source",
                "evaluated": evaluated,
                "source_attestation": source_attestation,
                "acted": {},
                "forward": {},
                "workspace_hash": self.WORKSPACE,
                "requesting_agent_id": self.AGENT,
            },
        )
        if not isinstance(source, dict) or not source.get("id"):
            raise VaultError("ephemeral fixture source event did not return an id")

        result = self._client.call_tool(
            "perseus_vault_remember",
            {
                "category": category,
                "key": key,
                "body_json": body_json,
                "type": "fact",
                "workspace_hash": self.WORKSPACE,
                "agent_id": self.AGENT,
                "actor_kind": "connector",
                "requesting_agent_id": self.AGENT,
                "skip_dedup": True,
                "admission": {
                    "record_digest": record_digest,
                    "source_identity": source_identity,
                    "source_event_id": source["id"],
                    "authorization_scope": self.WORKSPACE,
                    "ingestion_channel": "ephemeral-fixture",
                    "workspace_hash": self.WORKSPACE,
                    "source_trust": "authoritative",
                    "actor_kind": "connector",
                    "actor_identity": self.AGENT,
                    "validated": True,
                    "valid_from_unix_ms": 1,
                    "recorded_at_unix_ms": 2,
                    "task_relevance_bps": 9000,
                },
            },
        )
        if (
            not isinstance(result, dict)
            or result.get("serveable") is not True
            or result.get("proposed")
        ):
            raise VaultError(f"ephemeral fixture write was not serveable: {result}")
        return result

    def recall(
        self,
        query: str,
        *,
        category: Optional[str] = None,
        limit: int = 10,
        mode: str = "fts5",
    ) -> List[Dict[str, Any]]:
        """Recall fixture records with the fixture's exact workspace scope."""
        return self._client.recall(
            query,
            category=category,
            limit=limit,
            mode=mode,
            workspace_hash=self.WORKSPACE,
            requesting_agent_id=self.AGENT,
        )
