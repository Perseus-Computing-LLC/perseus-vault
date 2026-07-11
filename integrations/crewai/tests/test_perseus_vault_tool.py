"""Tests for the CrewAI PerseusVaultMemoryTool.

CrewAI (and its heavy dependency tree) need not be installed to exercise the
Perseus Vault wiring: we stub ``crewai.tools.BaseTool`` with a minimal base class, then
drive the tool against Perseus Vault's real MCP JSON-RPC envelope via a fake
VaultClient.
"""

from __future__ import annotations

import json
import sys
import types

import pytest


@pytest.fixture(scope="module")
def PerseusVaultMemoryTool():
    """Import PerseusVaultMemoryTool with a stubbed ``crewai.tools.BaseTool``."""
    if "crewai" not in sys.modules:
        crewai = types.ModuleType("crewai")
        tools = types.ModuleType("crewai.tools")

        class BaseTool:
            name: str = ""
            description: str = ""

            def __init__(self, *args, **kwargs):
                pass

        tools.BaseTool = BaseTool
        crewai.tools = tools
        sys.modules["crewai"] = crewai
        sys.modules["crewai.tools"] = tools

    from perseus_vault_crewai import PerseusVaultMemoryTool as tool_cls

    return tool_cls


def _make_fake_client(routes):
    """Build a fake ``VaultClient`` driven by ``routes``.

    ``routes`` maps a Perseus Vault tool name to a callable(arguments) -> payload
    dict. The tool now talks to the shared ``perseus_vault_client.VaultClient``
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
    # PerseusVaultMemoryTool builds a VaultClient lazily via _get_client(); swap the
    # class the module imported so the tool drives our fake transport.
    monkeypatch.setattr("perseus_vault_crewai.VaultClient", _make_fake_client(routes))


def test_remember_sends_type(monkeypatch, PerseusVaultMemoryTool):
    captured = {}

    def remember(args):
        captured.update(args)
        return {"id": "mem-1", "status": "ok"}

    _patch(monkeypatch, {"perseus_vault_remember": remember})
    tool = PerseusVaultMemoryTool()
    out = tool._remember(category="crewai", key="k1", content="hello world")

    assert captured.get("type") == "fact"  # regression: was the dropped "entity_type"
    assert "entity_type" not in captured
    assert json.loads(captured["body_json"]) == {"content": "hello world"}
    assert "Remembered" in out


def test_recall_parses_structured_items(monkeypatch, PerseusVaultMemoryTool):
    def recall(args):
        return {
            "items": [
                {
                    "category": "crewai",
                    "key": "k1",
                    "body_json": json.dumps({"content": "the answer is 42"}),
                }
            ],
            "total": 1,
        }

    _patch(monkeypatch, {"perseus_vault_recall": recall})
    tool = PerseusVaultMemoryTool()
    out = tool._recall(query="answer")

    # Before the envelope-unwrap fix this returned "No memories found".
    assert "Found 1 memory" in out
    assert "the answer is 42" in out


def test_unwrap_handles_text_only_envelope(PerseusVaultMemoryTool):
    assert PerseusVaultMemoryTool._unwrap_result(
        {"content": [{"type": "text", "text": json.dumps({"items": [1, 2]})}]}
    ) == {"items": [1, 2]}
