"""Tests for the PraisonAI PerseusVaultAdapter (#551).

PraisonAI need not be installed to exercise the Perseus Vault wiring: the
adapter's ``register()`` self-skips when PraisonAI is absent, and the memory
mapping is driven against a fake in-memory ``VaultClient`` (the shared stdio
transport lives in ``perseus_vault_client`` and is tested there). We patch the
``VaultClient`` symbol the adapter imported so no real ``perseus-vault`` binary
or subprocess is spawned.
"""

from __future__ import annotations

import json

import pytest

import perseus_vault_praison as pvp
from perseus_vault_praison import (
    PerseusVaultAdapter,
    SHORT_TERM_CATEGORY,
    LONG_TERM_CATEGORY,
)


class FakeVaultClient:
    """In-memory stand-in for perseus_vault_client.VaultClient.

    Implements only the typed helpers the adapter uses, with the same return
    shapes: ``remember`` echoes the key, ``recall``/``scan`` return normalized
    item dicts ({id, text, metadata, score, raw}), ``forget`` archives by key,
    ``prune`` clears a category.
    """

    def __init__(self, *args, **kwargs):
        # store: category -> {key: {"content":..., "metadata":...}}
        self.store: dict = {}
        self.closed = False

    def remember(self, category, key=None, body=None, **extra):
        self.store.setdefault(category, {})[key] = dict(body or {})
        return {"key": key, "action": "created"}

    def _items(self, category, query, limit, offset=0):
        rows = self.store.get(category, {})
        out = []
        for key, body in rows.items():
            content = body.get("content", "")
            if query and query.lower() not in content.lower():
                continue
            out.append({
                "id": key,
                "text": content,
                "metadata": body.get("metadata") or {},
                "score": 1.0,
                "raw": {"key": key, "category": category},
            })
        return out[offset:offset + limit]

    def recall(self, query, *, category=None, limit=10, mode="hybrid", offset=None, **extra):
        return self._items(category, query, limit, offset or 0)

    def scan(self, category, *, page_size=100, max_items=None):
        rows = self._items(category, "", 10_000)
        return rows[:max_items] if max_items is not None else rows

    def forget(self, category, key, *, reason=None):
        rows = self.store.get(category, {})
        if key in rows:
            del rows[key]
            return True
        return False

    def prune(self, category, *, purge_all=False, **extra):
        n = len(self.store.get(category, {}))
        if purge_all:
            self.store[category] = {}
        return {"archived": n}

    def get_entity(self, entity_id):
        for cat, rows in self.store.items():
            if entity_id in rows:
                return {"key": entity_id, "category": cat, "body_json": json.dumps(rows[entity_id])}
        return {}

    def close(self):
        self.closed = True


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(pvp, "VaultClient", FakeVaultClient)
    a = PerseusVaultAdapter(db_path=":memory:")
    yield a
    a.close()


def test_store_and_search_short_term_isolated_from_long(adapter):
    sid = adapter.store_short_term("ephemeral note about caching", {"source": "s"})
    lid = adapter.store_long_term("durable fact: user prefers metric units", {"source": "l"})
    assert sid and lid and sid != lid

    short = adapter.search_short_term("caching")
    assert len(short) == 1
    assert short[0]["type"] == "short"
    assert "caching" in short[0]["text"]

    # short-term search must NOT see the long-term row and vice versa
    assert adapter.search_short_term("metric") == []
    long = adapter.search_long_term("metric")
    assert len(long) == 1
    assert long[0]["type"] == "long"
    assert long[0]["metadata"] == {"source": "l"}


def test_get_all_memories_spans_both_categories(adapter):
    adapter.store_short_term("s1")
    adapter.store_short_term("s2")
    adapter.store_long_term("l1")
    allm = adapter.get_all_memories()
    assert len(allm) == 3
    types = sorted(m["type"] for m in allm)
    assert types == ["long", "short", "short"]


def test_get_all_memories_pages_past_the_100_cap(monkeypatch, adapter):
    # #562: enumeration must return the WHOLE category, not a single 100-cap page.
    for i in range(250):
        adapter.store_long_term(f"fact number {i}")
    allm = adapter.get_all_memories(page_size=100)
    assert len(allm) == 250


def test_get_context_assembles_relevant_text(adapter):
    adapter.store_long_term("Perseus Vault is a local-first memory backend")
    adapter.store_long_term("The sky is blue")
    ctx = adapter.get_context("memory backend", limit=5)
    assert "Perseus Vault" in ctx
    assert "sky is blue" not in ctx


def test_reset_clears_only_the_targeted_category(adapter):
    adapter.store_short_term("s1")
    adapter.store_long_term("l1")
    adapter.reset_short_term()
    assert adapter.search_short_term("s1") == []
    # long-term survives
    assert len(adapter.search_long_term("l1")) == 1


def test_delete_memory_and_delete_memories(adapter):
    a = adapter.store_long_term("alpha")
    b = adapter.store_long_term("beta")
    c = adapter.store_short_term("gamma")
    assert adapter.delete_memory(a, memory_type="long") is True
    # already gone
    assert adapter.delete_memory(a) is False
    deleted = adapter.delete_memories([b, c, "does-not-exist"])
    assert deleted == 2


def test_conforms_to_memory_protocol_when_praison_present():
    """If PraisonAI is installed, the adapter satisfies the runtime Protocol."""
    protocols = pytest.importorskip("praisonaiagents.memory.protocols")
    assert isinstance(PerseusVaultAdapter(db_path=":memory:"), protocols.MemoryProtocol)


def test_register_is_safe_without_praison(monkeypatch):
    """register() returns False (never raises) when PraisonAI is absent."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("praisonaiagents"):
            raise ImportError("praisonaiagents not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert pvp.register() is False
