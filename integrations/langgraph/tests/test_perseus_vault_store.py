"""Tests for PerseusVaultStore — the LangGraph BaseStore backed by Perseus Vault.

These mock the ``perseus-vault`` subprocess with the *real* MCP JSON-RPC envelope
Perseus Vault emits (``result.structuredContent`` / ``result.content[0].text``), so
they pin the response-parsing contract without needing the Rust binary.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from perseus_vault_langgraph import PerseusVaultStore


def _make_fake_client(routes):
    """Build a fake ``VaultClient`` driven by ``routes``.

    ``routes`` maps a Perseus Vault tool name to a callable(arguments) -> payload
    dict. The store now talks to the shared ``perseus_vault_client.VaultClient``
    (hardened stdio transport lives there), so we patch that seam instead of the
    subprocess: ``call_tool_raw`` returns the payload wrapped in Perseus Vault's
    real MCP envelope (``structuredContent`` + ``content[0].text``).
    """

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def call_tool_raw(self, name, arguments):
            payload = routes[name](arguments)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
            }

        def close(self):
            pass

    return FakeClient


def _patch(monkeypatch, routes):
    # PerseusVaultStore builds a VaultClient lazily via _get_client(); swap the
    # class the module imported so the store drives our fake transport.
    monkeypatch.setattr("perseus_vault_langgraph.VaultClient", _make_fake_client(routes))


def test_get_parses_structured_content(monkeypatch):
    routes = {
        "perseus_vault_recall": lambda a: {
            "items": [
                {
                    "key": "prefs",
                    "category": "users/123",
                    "body_json": json.dumps({"theme": "dark"}),
                    "created_at_unix_ms": 1700000000000,
                    "last_accessed_unix_ms": 1700000005000,
                    "decay_score": 0.9,
                }
            ],
            "total": 1,
        }
    }
    _patch(monkeypatch, routes)
    store = PerseusVaultStore()

    item = store.get(("users", "123"), "prefs")
    assert item is not None
    assert item.value == {"theme": "dark"}
    # Timestamps come back as real datetimes (Item.created_at is typed datetime).
    assert isinstance(item.created_at, datetime)
    assert item.created_at.year == 2023


def test_get_returns_none_when_no_match(monkeypatch):
    _patch(monkeypatch, {"perseus_vault_recall": lambda a: {"items": [], "total": 0}})
    store = PerseusVaultStore()
    assert store.get(("users", "123"), "missing") is None


def test_search_maps_items_and_score(monkeypatch):
    routes = {
        "perseus_vault_recall": lambda a: {
            "items": [
                {
                    "key": "n1",
                    "body_json": json.dumps({"text": "hello"}),
                    "created_at_unix_ms": 1700000000000,
                    "decay_score": 0.42,
                }
            ],
            "total": 1,
        }
    }
    _patch(monkeypatch, routes)
    store = PerseusVaultStore()

    results = store.search(("notes",), query="hello")
    assert len(results) == 1
    assert results[0].key == "n1"
    assert results[0].value == {"text": "hello"}
    assert results[0].score == 0.42


def test_put_sends_type_not_entity_type(monkeypatch):
    captured = {}

    def remember(args):
        captured.update(args)
        return {"id": "mem-1", "status": "ok"}

    _patch(monkeypatch, {"perseus_vault_remember": remember})
    store = PerseusVaultStore()

    store.put(("users", "123"), "prefs", {"theme": "dark"})
    assert captured["category"] == "users/123"
    assert captured["key"] == "prefs"
    assert json.loads(captured["body_json"]) == {"theme": "dark"}
    # Regression: Perseus Vault's param is ``type``; ``entity_type`` was silently dropped.
    assert captured.get("type") == "langgraph_item"
    assert "entity_type" not in captured


def test_list_namespaces_reads_by_category(monkeypatch):
    routes = {
        "perseus_vault_stats": lambda a: {
            "by_category": {"users/123": 3, "notes": 5, "default": 1}
        }
    }
    _patch(monkeypatch, routes)
    store = PerseusVaultStore()

    namespaces = store.list_namespaces()
    assert ("users", "123") in namespaces
    assert ("notes",) in namespaces


def test_unwrap_prefers_structured_then_text():
    # structuredContent wins when present.
    assert PerseusVaultStore._unwrap_result(
        {"structuredContent": {"items": [1]}, "content": [{"text": "{}"}]}
    ) == {"items": [1]}
    # Falls back to parsing content[0].text JSON.
    assert PerseusVaultStore._unwrap_result(
        {"content": [{"type": "text", "text": json.dumps({"items": [2]})}]}
    ) == {"items": [2]}
    # Garbage text yields an empty dict rather than blowing up.
    assert PerseusVaultStore._unwrap_result({"content": [{"text": "not json"}]}) == {}
