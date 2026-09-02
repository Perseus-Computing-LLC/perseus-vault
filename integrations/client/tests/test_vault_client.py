"""Tests for the Perseus Vault Python client.

Two layers:
- Fast unit tests drive a fake in-process transport (monkeypatched `_request` /
  fake stdio) so no binary is needed.
- Real-binary tests run only when a `perseus-vault` executable is discoverable
  (via PERSEUS_VAULT_BIN or PATH); otherwise they skip.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import textwrap
import threading

import pytest

from perseus_vault_client import VaultClient, VaultError, VaultTimeoutError


# ---------------------------------------------------------------------------
# Unit layer — fake transport (no subprocess)
# ---------------------------------------------------------------------------

class _FakeVault(VaultClient):
    """VaultClient with the transport replaced by an in-memory store, so the
    helper/normalization logic is testable without spawning a process."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.entities = {}   # (category, key) -> body
        self.calls = []

    # bypass process lifecycle entirely
    def _ensure_started(self):  # noqa: D401
        pass

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        short = name.split(self._prefix + "_", 1)[-1]

        if short == "remember":
            self.entities[(arguments["category"], arguments["key"])] = json.loads(arguments["body_json"])
            return {"action": "created", "key": arguments["key"]}
        if short == "recall":
            cat, q = arguments.get("category"), (arguments.get("query") or "").lower()
            limit = arguments.get("limit", 10)
            offset = arguments.get("offset", 0) or 0
            matched = []
            for (c, k), body in self.entities.items():
                if cat is not None and c != cat:
                    continue
                content = str(body.get("content", "")).lower()
                if q == "" or any(tok in content for tok in q.split()):
                    matched.append({"key": k, "body_json": json.dumps(body), "score": 0.5,
                                    "score_semantics": "fixture-relevance-v1"})
            # Honor offset + limit so paginated scan() terminates like the real vault.
            page = matched[offset:offset + limit]
            return {"items": page, "total": len(matched), "retrieval_profile": "hybrid"}
        if short == "scan":
            # Emulate the server-side keyset scan (#562): stable id order,
            # continuation cursor, has_more sentinel.
            cat = arguments.get("category")
            limit = arguments.get("limit", 100)
            cursor = arguments.get("cursor")
            rows = sorted(
                (f"{c}/{k}", k, body)
                for (c, k), body in self.entities.items()
                if cat is None or c == cat
            )
            if cursor:
                rows = [r for r in rows if r[0] > cursor]
            page = rows[:limit]
            has_more = len(rows) > limit
            return {
                "items": [
                    {"id": rid, "key": k, "body_json": json.dumps(b), "score": None}
                    for rid, k, b in page
                ],
                "total": len(page),
                "has_more": has_more,
                "next_cursor": page[-1][0] if has_more and page else None,
            }
        if short == "prune":
            cat = arguments.get("category")
            if arguments.get("purge_all"):
                doomed = [key for key in self.entities if key[0] == cat]
                for key in doomed:
                    del self.entities[key]
                return {"archived": len(doomed)}
            return {"archived": 0}
        if short == "forget":
            key = (arguments["category"], arguments["key"])
            existed = key in self.entities
            self.entities.pop(key, None)
            # Mirror the current server wire contract (#1024): {"found": bool, ...}
            return {
                "found": existed,
                "category": arguments["category"],
                "key": arguments["key"],
            }
        if short == "context":
            return {"markdown": "## Perseus Vault Context\n\n- (test)\n"}
        raise AssertionError(f"unexpected tool {name}")


def test_remember_generates_key_and_stores():
    v = _FakeVault()
    res = v.remember("architecture", body={"content": "SQLite + FTS5"})
    assert res["key"].startswith("architecture-")
    assert ("architecture", res["key"]) in v.entities


def test_remember_explicit_key_and_importance():
    v = _FakeVault()
    v.remember("decision", "use-pg", {"content": "postgres"}, importance=0.9)
    name, args = v.calls[-1]
    assert args["key"] == "use-pg"
    assert args["importance"] == 0.9


def test_recall_normalizes_items():
    v = _FakeVault()
    v.remember("architecture", "a", {"content": "blue-green deploy", "metadata": {"t": 1}})
    hits = v.recall("deploy", category="architecture")
    assert len(hits) == 1
    h = hits[0]
    assert h["id"] == "a"
    assert "blue-green" in h["text"]
    assert h["metadata"] == {"t": 1}
    assert isinstance(h["score"], float)
    assert "raw" in h


def test_recall_score_is_nullable_and_wire_rank_is_preserved():
    v = _FakeVault()
    v.remember("c", "k", {"content": "no score provided"})
    normalized = VaultClient._normalize_recall_response({
        "items": [
            {"key": "x", "body_json": json.dumps({"content": "hi"}), "decay_score": 0.1},
            {"key": "y", "body_json": json.dumps({"content": "bye"}), "decay_score": 0.9},
        ],
        "total": 2,
        "retrieval_profile": "fixture",
    }, limit=2)
    assert normalized[0]["score"] is None
    assert normalized[0]["wire_rank"] == 1
    assert normalized[1]["wire_rank"] == 2
    assert normalized[0]["metadata"] == {}


@pytest.mark.parametrize("offset", [False, 0.0, ""])
def test_recall_rejects_invalid_non_none_offset_before_transport(offset):
    v = _FakeVault()
    with pytest.raises(VaultError):
        v.recall("anything", offset=offset)
    assert v.calls == []


def test_recall_rejects_offset_past_total():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {"items": [], "total": 1, "retrieval_profile": "fixture"},
            limit=1,
            offset=2,
        )


def test_client_accepts_actual_entity_transport_projection():
    response = {
        "items": [{
            "id": "entity-1",
            "category": "facts",
            "key": "fact-1",
            "body_json": {"content": "expanded transport body", "summary": "fixture"},
            "content": "expanded transport body",
            "summary": "fixture",
            "status": "active",
            "type": "insight",
            "tags": [],
            "decay_score": 0.42,
            "retrieval_count": 1,
            "layer": "working",
            "topic_path": "",
            "archived": False,
            "archive_reason": "",
            "links": [],
            "verified": False,
            "source": "agent",
            "always_on": False,
            "certainty": 0.5,
            "workspace_hash": "",
            "agent_id": "",
            "visibility": "workspace",
            "created_at_unix_ms": 1700000000000,
            "last_accessed_unix_ms": 1700000005000,
            "follow_count": 0,
            "miss_count": 0,
            "follow_rate": 0.0,
            "efficacy_status": "unverified",
            "epistemic_state": "candidate",
            "hints": [],
            "memory_type": "",
            "encoding_strength": "S1",
            "why_served": {
                "reason": "matched the recall query",
                "memory_class": "facts",
                "promotion_state": "unpromoted",
                "support_count": 0,
                "source_evidence_ids": [],
                "promoted_scope": "",
            },
            "untrusted": True,
            "untrusted_reason": "epistemic_state:candidate",
        }],
        "total": 1,
        "retrieval_profile": "fixture",
    }
    normalized = VaultClient._normalize_recall_response(response, limit=1)
    assert normalized[0]["id"] == "fact-1"
    assert normalized[0]["text"] == "expanded transport body"


def test_recall_rejects_limits_beyond_protocol_cap_before_transport():
    v = _FakeVault()
    with pytest.raises(VaultError):
        v.recall("anything", limit=1001)
    assert v.calls == []

    with pytest.raises(VaultError):
        v.semantic_search("anything", limit=1001)
    assert v.calls == []

    with pytest.raises(VaultError):
        v.scan("anything", page_size=1001)
    assert v.calls == []


def test_recall_rejects_timestamps_beyond_protocol_horizon():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{
                    "key": "x",
                    "body_json": {"content": "x"},
                    "created_at_unix_ms": 10**16,
                }],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )


def test_recall_huge_integer_scores_fail_closed_without_overflow_escape():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{
                    "key": "x",
                    "body_json": {"content": "x"},
                    "score": 10**400,
                    "score_semantics": "fixture-relevance-v1",
                }],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )


def test_recall_accepts_serialized_completeness_enum():
    response = {
        "items": [],
        "total": 0,
        "retrieval_profile": "fixture",
        "outcome": {
            "status": "empty",
            "abstained": True,
            "completeness": "Abstain",
        },
    }
    assert VaultClient._normalize_recall_response(response, limit=1) == []


def test_recall_accepts_empty_partial_outcome():
    response = {
        "items": [],
        "total": 0,
        "retrieval_profile": "fixture",
        "outcome": {
            "status": "partial",
            "abstained": False,
            "reason": "partial_arms",
            "completeness": "Abstain",
        },
    }
    assert VaultClient._normalize_recall_response(response, limit=1) == []


@pytest.mark.parametrize("field", ["evidence", "fused_trace", "outcome"])
def test_recall_optional_projection_roots_must_be_objects(field):
    response = {
        "items": [{"key": "x", "body_json": {"content": "x"}}],
        "total": 1,
        "retrieval_profile": "fixture",
        field: [],
    }
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(response, limit=1)


def test_recall_nested_projection_objects_must_remain_objects():
    response = {
        "items": [{"key": "x", "body_json": {"content": "x"}}],
        "total": 1,
        "retrieval_profile": "fixture",
        "outcome": {
            "status": "fresh",
            "backend_health": [],
        },
    }
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(response, limit=1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "users/123"),
        ("created_at_unix_ms", 1700000000000),
        ("updated_at_unix_ms", 1700000004000),
        ("last_accessed_unix_ms", 1700000005000),
    ],
)
def test_recall_preserves_bounded_server_metadata_fields(field, value):
    normalized = VaultClient._normalize_recall_response(
        {
            "items": [{
                "key": "x",
                "body_json": {"content": "x"},
                field: value,
            }],
            "total": 1,
            "retrieval_profile": "fixture",
        },
        limit=1,
    )
    assert normalized[0]["raw"][field] == value


def test_recall_malformed_response_fails_closed():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response({
            "items": [{"key": "x", "score": "0.5"}],
            "total": 1,
            "retrieval_profile": "fixture",
        }, limit=1)


@pytest.mark.parametrize("body", [None, 0, [], "not-json", "null", "[]"])
def test_recall_invalid_body_json_fails_closed(body):
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{"key": "x", "body_json": body}],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )


def test_recall_rejects_mismatched_flattened_body_field():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{
                    "key": "x",
                    "body_json": {"content": "x", "custom": "canonical"},
                    "custom": "tampered",
                }],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )


def test_recall_rejects_json_type_confused_flattened_body_field():
    for flattened in (True, 1.0):
        with pytest.raises(VaultError):
            VaultClient._normalize_recall_response(
                {
                    "items": [{
                        "key": "x",
                        "body_json": {"content": "x", "count": 1},
                        "count": flattened,
                    }],
                    "total": 1,
                    "retrieval_profile": "fixture",
                },
                limit=1,
            )


def test_recall_missing_body_json_fails_closed():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{"key": "x"}],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )


def test_recall_empty_key_does_not_fall_back_to_id():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{"key": "", "id": "fallback", "body_json": {"content": "x"}}],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )


def test_recall_pagination_preserves_global_wire_rank():
    normalized = VaultClient._normalize_recall_response(
        {
            "items": [
                {"key": "x", "body_json": {"content": "x"}, "wire_rank": 11},
                {"key": "y", "body_json": {"content": "y"}, "wire_rank": 12},
            ],
            "total": 12,
            "retrieval_profile": "fixture",
        },
        limit=2,
        offset=10,
    )
    assert [item["wire_rank"] for item in normalized] == [11, 12]


def test_recall_item_projection_and_duplicate_keys_fail_closed():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [
                    {"key": "x", "body_json": {"content": "x"}, "private_projection": {}},
                ],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
        )
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [
                    {"key": "x", "body_json": {"content": "x"}},
                    {"key": "x", "body_json": {"content": "x"}},
                ],
                "total": 2,
                "retrieval_profile": "fixture",
            },
            limit=2,
        )


def test_recall_expansion_accepts_variants_without_profile():
    normalized = VaultClient._normalize_recall_response({
        "items": [{"key": "expanded", "body_json": json.dumps({"content": "x"})}],
        "total": 1,
        "variants": 2,
    }, limit=1)
    assert normalized[0]["wire_rank"] == 1


def test_recall_score_requires_explicit_semantics():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response({
            "items": [{"key": "x", "score": 0.5}],
            "total": 1,
            "retrieval_profile": "fixture",
        })


@pytest.mark.parametrize("status", ["timeout", "stale", "unknown", "unavailable"])
def test_recall_unsafe_outcomes_fail_closed(status):
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response({
            "items": [{"key": "x"}],
            "total": 1,
            "retrieval_profile": "fixture",
            "outcome": {"status": status},
        }, limit=1)


def test_recall_optional_projection_shape_is_strict():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response({
            "items": [{"key": "x"}],
            "total": 1,
            "retrieval_profile": "fixture",
            "evidence": [],
        }, limit=1)

def test_recall_nested_projection_unknown_fields_fail_closed():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{"key": "x", "body_json": {"content": "x"}}],
                "total": 1,
                "retrieval_profile": "fixture",
                "diagnostic": {
                    "reason": "no_match",
                    "active_memories": 0,
                    "RAW-QUERY-SENTINEL": "must-not-cross",
                },
            },
            limit=1,
        )


def test_recall_page_cannot_extend_past_total():
    with pytest.raises(VaultError):
        VaultClient._normalize_recall_response(
            {
                "items": [{"key": "x", "body_json": {"content": "x"}, "wire_rank": 11}],
                "total": 1,
                "retrieval_profile": "fixture",
            },
            limit=1,
            offset=10,
        )


def test_recall_requires_explicit_wire_envelope():
    v = _FakeVault()
    v.call_tool = lambda name, arguments: {"items": [{"key": "x", "body_json": "{}"}], "total": 1}
    with pytest.raises(VaultError, match="retrieval_profile"):
        v.recall("x")


def test_forget_true_only_when_found():
    v = _FakeVault()
    v.remember("c", "k", {"content": "bye"})
    assert v.forget("c", "k") is True
    assert v.forget("c", "missing") is False


def test_forget_legacy_archived_response_still_supported():
    # Pre-2.x servers answered {"archived": N}; keep that contract working (#1024).
    v = _FakeVault()
    v.call_tool = lambda name, arguments: {"archived": 1}
    assert v.forget("c", "k") is True
    v.call_tool = lambda name, arguments: {"archived": 0}
    assert v.forget("c", "k") is False


def test_forget_non_dict_response_is_false():
    v = _FakeVault()
    v.call_tool = lambda name, args: "archived"  # non-dict, ambiguous
    assert v.forget("c", "k") is False


def test_scan_paginates_full_category():
    v = _FakeVault()
    for i in range(250):
        v.remember("bulk", f"k{i}", {"content": f"item number {i}"})
    got = v.scan("bulk", page_size=100)
    assert len(got) == 250  # all pages, not truncated at 100


def test_scan_respects_max_items():
    v = _FakeVault()
    for i in range(50):
        v.remember("bulk", f"k{i}", {"content": f"item {i}"})
    assert len(v.scan("bulk", page_size=10, max_items=25)) == 25


def test_scan_falls_back_on_pre562_server():
    # A server without the scan tool answers with an isError text payload,
    # which call_tool surfaces as a plain string — scan() must fall back to
    # legacy offset-paged empty-query recall and still return everything.
    v = _FakeVault()
    for i in range(30):
        v.remember("bulk", f"k{i}", {"content": f"item number {i}"})
    orig = v.call_tool
    def call_tool(name, args):
        if name.endswith("_scan"):
            return "Unknown tool: perseus_vault_scan"
        return orig(name, args)
    v.call_tool = call_tool
    assert len(v.scan("bulk", page_size=10)) == 30


def test_prune_purge_all_scopes_to_category():
    v = _FakeVault()
    v.remember("working", "w1", {"content": "scratch"})
    v.remember("episodic", "e1", {"content": "durable"})
    v.prune("working", purge_all=True)
    assert v.recall("scratch", category="working") == []
    assert len(v.recall("durable", category="episodic")) == 1


def test_context_returns_markdown_string():
    v = _FakeVault()
    assert v.context(query="x").startswith("## Perseus Vault Context")


def test_call_tool_prefers_structured_content():
    # When both structuredContent and a text block are present, structuredContent wins.
    v = VaultClient(binary="x", db_path="y")
    v._request = lambda method, params: {
        "content": [{"type": "text", "text": '{"from":"text"}'}],
        "structuredContent": {"from": "structured", "items": [1, 2]},
    }
    assert v.call_tool("perseus_vault_recall", {}) == {"from": "structured", "items": [1, 2]}


def test_call_tool_falls_back_to_text_block():
    v = VaultClient(binary="x", db_path="y")
    v._request = lambda method, params: {
        "content": [{"type": "text", "text": '{"from":"text"}'}],
    }
    assert v.call_tool("perseus_vault_recall", {}) == {"from": "text"}


def test_call_tool_raw_returns_envelope():
    v = VaultClient(binary="x", db_path="y")
    envelope = {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"ok": True}}
    v._request = lambda method, params: envelope
    assert v.call_tool_raw("perseus_vault_health", {}) == envelope


def test_extra_args_are_included_in_serve_command(monkeypatch):
    monkeypatch.delenv("PERSEUS_VAULT_ENCRYPTION_KEY", raising=False)
    captured = {}

    class FakeProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    client = VaultClient(
        binary="/opt/pv/perseus-vault",
        db_path="/data/agent.db",
        extra_args=["--llm-endpoint", "http://127.0.0.1:11434", "--llm-model", "embed"],
    )
    client._request = lambda method, params: {}
    client._notify = lambda method, params: None
    client._start()
    assert captured["command"] == [
        "/opt/pv/perseus-vault",
        "serve",
        "--db",
        "/data/agent.db",
        "--llm-endpoint",
        "http://127.0.0.1:11434",
        "--llm-model",
        "embed",
    ]


# ---------------------------------------------------------------------------
# Transport layer — real subprocess behaviors with a fake "binary"
# ---------------------------------------------------------------------------

def _spawn_with_script(script: str, timeout: float = 1.0) -> VaultClient:
    """Build a VaultClient whose child process runs `script` (a python program)
    instead of the real binary, and complete the handshake."""
    client = VaultClient(binary=sys.executable, db_path="unused", timeout=timeout)
    # Override _start to launch our script directly (ignoring serve/--db args).
    def _start():
        client._proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=client._env,
        )
        client._request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}})
        client._notify("notifications/initialized", {})
    client._start = _start  # type: ignore
    client._ensure_started()
    return client


_ECHO_SERVER = textwrap.dedent('''
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        msg = json.loads(line)
        if "id" not in msg:  # notification
            continue
        method = msg.get("method")
        if method == "tools/call":
            body = {"content":[{"type":"text","text": json.dumps({"echo": msg["params"]["arguments"]})}]}
            sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":body})+"\\n")
        else:
            sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}})+"\\n")
        sys.stdout.flush()
''')

_HANG_AFTER_INIT = textwrap.dedent('''
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        msg = json.loads(line)
        if msg.get("method") == "initialize":
            sys.stdout.write(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{}})+"\\n")
            sys.stdout.flush()
        # any later request: never respond
''')


def test_roundtrip_over_real_stdio():
    client = _spawn_with_script(_ECHO_SERVER)
    try:
        res = client.call_tool("perseus_vault_remember", {"category": "c", "key": "k"})
        assert res == {"echo": {"category": "c", "key": "k"}}
    finally:
        client.close()


def test_timeout_tears_down_process():
    client = _spawn_with_script(_HANG_AFTER_INIT, timeout=1.0)
    proc = client._proc
    with pytest.raises(VaultTimeoutError):
        client.call_tool("perseus_vault_health", {})
    proc.wait(timeout=5)
    assert proc.poll() is not None      # child terminated on timeout
    assert client._proc is None         # reset for a clean respawn
    client.close()


def test_reentrant_handshake_no_deadlock():
    # If the lock were non-reentrant, _spawn_with_script's handshake (which calls
    # _request while _start holds the lock) would deadlock and hang the test.
    client = _spawn_with_script(_ECHO_SERVER)
    client.close()


def test_missing_binary_raises_vaulterror():
    client = VaultClient(binary="/nonexistent/perseus-vault-xyz", db_path="x")
    with pytest.raises(VaultError):
        client.list_tools()


# ---------------------------------------------------------------------------
# Real binary (skipped unless perseus-vault is available)
# ---------------------------------------------------------------------------

_REAL_BIN = os.getenv("PERSEUS_VAULT_BIN") or shutil.which("perseus-vault")


@pytest.mark.skipif(not _REAL_BIN, reason="perseus-vault binary not available")
def test_real_binary_store_recall(tmp_path):
    db = str(tmp_path / "real.db")
    with VaultClient(binary=_REAL_BIN, db_path=db) as vault:
        assert vault.health().get("status") == "healthy"
        result = vault.remember("architecture", "use-sqlite", {"content": "SQLite FTS5 index"})
        assert result.get("proposed") is True
        assert result.get("serveable") is not True
        assert vault.recall("database index", category="architecture", limit=5) == []
        vault.prune("architecture", purge_all=True)
        assert vault.recall("", category="architecture") == []


@pytest.mark.skipif(not _REAL_BIN, reason="perseus-vault binary not available")
def test_ephemeral_fixture_admits_and_recalls_then_cleans_up(tmp_path):
    from perseus_vault_client import EphemeralAdmissionFixture

    with EphemeralAdmissionFixture(binary=_REAL_BIN) as fixture:
        db_path = fixture.db_path
        assert Path(db_path).parent.name.startswith("perseus-vault-ephemeral-")
        result = fixture.remember(
            "integration-fixture",
            "deterministic",
            {"content": "ephemeral fixture record"},
        )
        assert result.get("serveable") is True
        assert any(
            item["id"] == "deterministic"
            for item in fixture.recall("ephemeral fixture", category="integration-fixture")
        )

    assert not Path(db_path).exists()


@pytest.mark.skipif(not _REAL_BIN, reason="perseus-vault binary not available")
def test_real_binary_forget_reports_found(tmp_path):
    # #1024: forget() must report success against the real wire contract
    # {"found": true|false} — not the legacy {"archived": N} shape.
    db = str(tmp_path / "forget.db")
    with VaultClient(binary=_REAL_BIN, db_path=db) as vault:
        vault.remember("scratch", "bye", {"content": "temporary"})
        assert vault.forget("scratch", "bye") is True
        # Forgetting an already-archived entity reports found=false, not an error.
        assert vault.forget("scratch", "bye") is False
