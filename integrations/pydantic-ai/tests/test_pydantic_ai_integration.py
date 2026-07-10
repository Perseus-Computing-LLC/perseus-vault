"""Tests for the Perseus Vault Pydantic AI integration.

- Structural tests build the capability/toolset and assert config resolution +
  correct wiring, without spawning the binary.
- A real-binary end-to-end test runs an Agent that discovers and calls the
  vault's tools; it skips unless a `perseus-vault` binary is available.
"""

import os
import shutil

import pytest

from pydantic_ai.capabilities import MCP, AbstractCapability
from pydantic_ai.mcp import MCPToolset, StdioTransport

from perseus_vault_pydantic_ai import (
    perseus_vault_capability,
    perseus_vault_toolset,
)


# ---------------------------------------------------------------------------
# Structural (no subprocess)
# ---------------------------------------------------------------------------

def test_capability_type_and_id():
    cap = perseus_vault_capability(binary="perseus-vault", db_path="./x.db")
    assert isinstance(cap, MCP)
    assert isinstance(cap, AbstractCapability)
    assert cap.id == "perseus_vault"
    assert cap.get_serialization_name() == "MCP"


def test_capability_wraps_a_toolset():
    cap = perseus_vault_capability(db_path="./x.db")
    assert isinstance(cap.get_toolset(), MCPToolset)


def test_toolset_type():
    ts = perseus_vault_toolset(binary="perseus-vault", db_path="./x.db")
    assert isinstance(ts, MCPToolset)


def test_stdio_transport_command_and_args():
    ts = perseus_vault_toolset(
        binary="/opt/pv/perseus-vault",
        db_path="/data/agent.db",
        encryption_key="/keys/aes.key",
        extra_args=["--offline"],
    )
    transport = _extract_transport(ts)
    assert isinstance(transport, StdioTransport)
    assert transport.command == "/opt/pv/perseus-vault"
    assert transport.args[:3] == ["serve", "--db", "/data/agent.db"]
    assert "--encryption-key" in transport.args
    assert "/keys/aes.key" in transport.args
    assert transport.args[-1] == "--offline"


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("PERSEUS_VAULT_BIN", "/env/perseus-vault")
    monkeypatch.setenv("PERSEUS_VAULT_DB", "/env/vault.db")
    monkeypatch.setenv("PERSEUS_VAULT_ENCRYPTION_KEY", "/env/key")
    ts = perseus_vault_toolset()
    transport = _extract_transport(ts)
    assert transport.command == "/env/perseus-vault"
    assert "/env/vault.db" in transport.args
    assert "/env/key" in transport.args


def test_allowed_tools_forwarded():
    cap = perseus_vault_capability(
        db_path="./x.db",
        allowed_tools=["perseus_vault_remember", "perseus_vault_recall"],
    )
    # The MCP capability stores the allow-list; exact attr name is internal, so
    # assert via repr/dict to stay robust across pydantic-ai versions.
    blob = repr(cap)
    assert "perseus_vault_remember" in blob and "perseus_vault_recall" in blob


def _extract_transport(ts: MCPToolset) -> StdioTransport:
    """Find the StdioTransport inside an MCPToolset across pydantic-ai versions."""
    for attr in ("_client", "client", "_transport", "transport"):
        obj = getattr(ts, attr, None)
        if isinstance(obj, StdioTransport):
            return obj
        # client may itself hold the transport
        for sub in ("transport", "_transport"):
            t = getattr(obj, sub, None)
            if isinstance(t, StdioTransport):
                return t
    # Last resort: scan __dict__ recursively one level.
    for v in vars(ts).values():
        if isinstance(v, StdioTransport):
            return v
        for vv in (vars(v).values() if hasattr(v, "__dict__") else []):
            if isinstance(vv, StdioTransport):
                return vv
    raise AssertionError("StdioTransport not found in MCPToolset")


# ---------------------------------------------------------------------------
# Real binary end-to-end (skipped unless perseus-vault is available)
# ---------------------------------------------------------------------------

_REAL_BIN = os.getenv("PERSEUS_VAULT_BIN") or shutil.which("perseus-vault")


@pytest.mark.skipif(not _REAL_BIN, reason="perseus-vault binary not available")
def test_agent_discovers_and_calls_vault_tools(tmp_path):
    import asyncio
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel, AgentInfo
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    db = str(tmp_path / "e2e.db")
    cap = perseus_vault_capability(binary=_REAL_BIN, db_path=db)

    seen = {"step": 0, "tools": []}

    def model_fn(messages, info: AgentInfo):
        seen["tools"] = [t.name for t in info.function_tools]
        step = seen["step"]
        seen["step"] += 1
        if step == 0:
            remember = [n for n in seen["tools"] if n.endswith("remember")]
            if remember:
                return ModelResponse(parts=[ToolCallPart(
                    remember[0],
                    {"category": "architecture", "key": "e2e",
                     "body_json": '{"content":"pydantic-ai integration works"}'},
                )])
        return ModelResponse(parts=[TextPart("done")])

    async def run():
        agent = Agent(FunctionModel(model_fn), capabilities=[cap])
        async with agent:
            return await agent.run("store a memory")

    result = asyncio.run(run())
    pv_tools = [n for n in seen["tools"] if n.startswith("perseus_vault_")]
    assert len(pv_tools) >= 20, f"expected vault tools, saw {seen['tools'][:5]}"
    assert "perseus_vault_remember" in pv_tools
    assert result.output == "done"
