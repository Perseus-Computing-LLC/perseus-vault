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
                    matched.append({"key": k, "body_json": json.dumps(body), "score": 0.5})
            # Honor offset + limit so paginated scan() terminates like the real vault.
            page = matched[offset:offset + limit]
            return {"items": page, "total": len(matched)}
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
            return {"archived": 1 if existed else 0}
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


def test_recall_score_never_none():
    v = _FakeVault()
    v.remember("c", "k", {"content": "no score provided"})
    # fake returns score 0.5; force a None-score item through normalization
    normalized = VaultClient._normalize_items({"items": [{"key": "x", "body_json": json.dumps({"content": "hi"}), "score": None}]})
    assert normalized[0]["score"] == 0.0
    assert normalized[0]["metadata"] == {}


def test_forget_true_only_when_archived():
    v = _FakeVault()
    v.remember("c", "k", {"content": "bye"})
    assert v.forget("c", "k") is True
    assert v.forget("c", "missing") is False


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
        vault.remember("architecture", "use-sqlite", {"content": "SQLite FTS5 index"})
        hits = vault.recall("database index", category="architecture", limit=5)
        assert any("SQLite" in h["text"] for h in hits)
        vault.prune("architecture", purge_all=True)
        assert vault.recall("", category="architecture") == []
