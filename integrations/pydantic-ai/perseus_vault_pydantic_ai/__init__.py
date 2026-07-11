"""Perseus Vault integration for Pydantic AI.

Wires a local ``perseus-vault`` MCP server into a Pydantic AI agent as a
first-class [capability](https://ai.pydantic.dev). Perseus Vault is a single
static binary (SQLite + FTS5 + bundled ONNX embeddings, optional AES-256-GCM),
so this gives an agent persistent, local-first, offline memory with no external
service.

This intentionally builds on Pydantic AI's **native** MCP machinery
(`StdioTransport` -> `MCPToolset` -> the `MCP` capability) rather than a bespoke
transport: the vault speaks MCP JSON-RPC over stdio, and Pydantic AI already
knows how to drive that idiomatically (tracing, tool caching, lifecycle). The
agent discovers the vault's tools (``perseus_vault_remember`` /
``perseus_vault_recall`` / ``perseus_vault_semantic_search`` / … — 55+ tools)
and calls them like any other tool.

Example
-------
>>> from pydantic_ai import Agent
>>> from perseus_vault_pydantic_ai import perseus_vault_capability
>>>
>>> memory = perseus_vault_capability(binary="perseus-vault", db_path="./agent.db")
>>> agent = Agent("openai:gpt-5", capabilities=[memory])
>>> async with agent:
...     result = await agent.run("Remember that I prefer metric units.")

The returned object is a plain ``pydantic_ai.capabilities.MCP`` instance, so it
composes with any other capabilities and is serialized under the ``MCP``
capability name.
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from pydantic_ai.capabilities import MCP
from pydantic_ai.mcp import MCPToolset, StdioTransport

__all__ = ["perseus_vault_capability", "perseus_vault_toolset"]

__version__ = "0.1.0"

# Canonical tool namespace advertised by perseus-vault >= 2.20.0.
_DEFAULT_ID = "perseus_vault"


def _resolve(binary: Optional[str], db_path: Optional[str], encryption_key: Optional[str]):
    binary = binary or os.getenv("PERSEUS_VAULT_BIN", "perseus-vault")
    db_path = db_path or os.getenv("PERSEUS_VAULT_DB", "./perseus-vault.db")
    encryption_key = encryption_key or os.getenv("PERSEUS_VAULT_ENCRYPTION_KEY")
    return binary, db_path, encryption_key


def _build_transport(
    binary: str,
    db_path: str,
    encryption_key: Optional[str],
    env: Optional[dict],
    extra_args: Optional[List[str]],
) -> StdioTransport:
    args = ["serve", "--db", db_path]
    if encryption_key:
        args += ["--encryption-key", encryption_key]
    if extra_args:
        args += list(extra_args)
    return StdioTransport(command=binary, args=args, env=env)


def perseus_vault_toolset(
    *,
    binary: Optional[str] = None,
    db_path: Optional[str] = None,
    encryption_key: Optional[str] = None,
    env: Optional[dict] = None,
    extra_args: Optional[List[str]] = None,
    **toolset_kwargs: Any,
) -> MCPToolset:
    """Build a Pydantic AI ``MCPToolset`` backed by a local ``perseus-vault serve``.

    Useful when you want to pass the toolset directly to ``Agent(toolsets=[...])``
    instead of using the capability wrapper. Prefer
    :func:`perseus_vault_capability` for the capability-based API.

    Config falls back to ``PERSEUS_VAULT_BIN`` / ``PERSEUS_VAULT_DB`` /
    ``PERSEUS_VAULT_ENCRYPTION_KEY`` environment variables.
    """
    binary, db_path, encryption_key = _resolve(binary, db_path, encryption_key)
    transport = _build_transport(binary, db_path, encryption_key, env, extra_args)
    return MCPToolset(transport, **toolset_kwargs)


def perseus_vault_capability(
    *,
    binary: Optional[str] = None,
    db_path: Optional[str] = None,
    encryption_key: Optional[str] = None,
    env: Optional[dict] = None,
    extra_args: Optional[List[str]] = None,
    id: str = _DEFAULT_ID,
    allowed_tools: Optional[List[str]] = None,
    **mcp_kwargs: Any,
) -> MCP:
    """Return an ``MCP`` capability that gives a Pydantic AI agent persistent,
    local-first memory backed by a Perseus Vault MCP server.

    Parameters
    ----------
    binary:
        Path to the ``perseus-vault`` executable. Falls back to
        ``PERSEUS_VAULT_BIN`` env, then ``"perseus-vault"`` on ``PATH``.
    db_path:
        SQLite DB path. Falls back to ``PERSEUS_VAULT_DB`` env, then
        ``"./perseus-vault.db"``.
    encryption_key:
        Optional path to an AES-256-GCM key file. Falls back to
        ``PERSEUS_VAULT_ENCRYPTION_KEY`` env.
    env:
        Extra environment variables for the vault subprocess.
    extra_args:
        Extra CLI args appended after ``serve --db <path>``.
    id:
        Capability id (default ``"perseus_vault"``).
    allowed_tools:
        Optional allow-list of tool names to expose to the agent. Omit to expose
        the full vault tool surface.
    **mcp_kwargs:
        Forwarded to :class:`pydantic_ai.capabilities.MCP` (e.g. ``description``,
        ``defer_loading``).

    Returns
    -------
    pydantic_ai.capabilities.MCP
        Pass it via ``Agent(capabilities=[...])``.
    """
    toolset = perseus_vault_toolset(
        binary=binary, db_path=db_path, encryption_key=encryption_key,
        env=env, extra_args=extra_args,
    )
    kwargs: dict = {"local": toolset, "id": id}
    if allowed_tools is not None:
        kwargs["allowed_tools"] = allowed_tools
    kwargs.update(mcp_kwargs)
    return MCP(**kwargs)
