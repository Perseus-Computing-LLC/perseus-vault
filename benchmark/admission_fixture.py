"""Shared admitted-write fixture for benchmark harnesses.

The production MCP boundary treats transport-authenticated writes without an
admission envelope as reviewable proposals. Benchmarks that need serveable
facts must construct the same hash-only source binding used by production,
never bypass the lifecycle gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

WORKSPACE = "perseus-benchmark"
AGENT = "perseus-benchmark"
HMAC_KEY = "perseus-benchmark-fixture-key"


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def child_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env["PERSEUS_VAULT_ADMISSION_SOURCE_HMAC_KEY"] = HMAC_KEY
    return env


def configure(client: Any) -> None:
    client.call(
        "perseus_vault_agent",
        {"agent_id": AGENT, "name": AGENT, "trust_tier": 2, "fleet_id": "benchmark"},
    )
    client.call(
        "perseus_vault_authority_set",
        {
            "agent_id": AGENT,
            "workspace_hash": WORKSPACE,
            "allowed_capabilities": [
                "memory.admission.source",
                "memory.commit",
                "memory.read",
                "memory.write",
                "memory.maintenance",
                "memory.delete",
                "memory.export",
            ],
            "scope_anchors": [WORKSPACE],
            "mode": "enforce",
            "author_agent_id": "operator",
            "capability_constraints_json": "{}",
        },
    )


def admitted_remember(client: Any, category: str, key: str, body_json: str) -> dict[str, Any]:
    body = stable_json(json.loads(body_json))
    record_digest = hashlib.sha256(body.encode()).hexdigest()
    evaluated = {
        "record_digest": record_digest,
        "source_identity": f"{category}:{key}",
        "workspace_hash": WORKSPACE,
        "actor_kind": "connector",
        "actor_identity": AGENT,
    }
    attestation_payload = stable_json({**evaluated, "requesting_agent_id": AGENT})
    source_attestation = hmac.new(
        HMAC_KEY.encode(), attestation_payload.encode(), hashlib.sha256
    ).hexdigest()
    source = client.call(
        "perseus_vault_journal",
        {
            "event_type": "admission_source",
            "evaluated": evaluated,
            "source_attestation": source_attestation,
            "acted": {},
            "forward": {},
            "workspace_hash": WORKSPACE,
        },
    )
    result = client.call(
        "perseus_vault_remember",
        {
            "category": category,
            "key": key,
            "body_json": body,
            "type": "fact",
            "workspace_hash": WORKSPACE,
            "agent_id": AGENT,
            "actor_kind": "connector",
            "requesting_agent_id": AGENT,
            "skip_dedup": True,
            "admission": {
                "record_digest": record_digest,
                "source_identity": evaluated["source_identity"],
                "source_event_id": source["id"],
                "authorization_scope": WORKSPACE,
                "ingestion_channel": "benchmark",
                "workspace_hash": WORKSPACE,
                "source_trust": "authoritative",
                "actor_kind": "connector",
                "actor_identity": AGENT,
                "validated": True,
                "valid_from_unix_ms": 1,
                "recorded_at_unix_ms": 2,
                "task_relevance_bps": 9000,
            },
        },
    )
    if not isinstance(result, dict) or result.get("serveable") is not True or result.get("proposed"):
        raise RuntimeError(f"admitted benchmark write was not serveable: {result}")
    return result
