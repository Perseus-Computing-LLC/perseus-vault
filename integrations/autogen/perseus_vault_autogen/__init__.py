"""
PerseusVaultMemory — AutoGen ``Memory`` implementation backed by Perseus Vault.

Drop-in persistent long-term memory for AutoGen (AG2 / autogen-core v0.4+)
agents. Implements the ``autogen_core.memory.Memory`` protocol so a Perseus Vault
database can be attached to any ``AssistantAgent`` and its stored knowledge is
injected into the model context before each inference.

Usage:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_ext.models.openai import OpenAIChatCompletionClient
    from perseus_vault_autogen import PerseusVaultMemory

    memory = PerseusVaultMemory(db_path="~/.perseus-vault/data/agent.db")

    agent = AssistantAgent(
        name="assistant",
        model_client=OpenAIChatCompletionClient(model="gpt-4o"),
        memory=[memory],
    )

The adapter maps AutoGen's ``MemoryContent`` model onto Perseus Vault's entity model:

    MemoryContent.content   → Perseus Vault body_json {"content": ...}
    MemoryContent.metadata  → merged into body_json (category/key extracted)
    query(text)             → Perseus Vault FTS5 recall
    update_context()        → prepends a Perseus Vault context block as a SystemMessage

It keeps a persistent Perseus Vault stdio session — the process is spawned once and
reused across all calls, avoiding per-call cold-start overhead (process spawn +
DB open + init handshake).
"""

from __future__ import annotations

import json
import time
import logging
from pathlib import Path
from typing import Any, Optional

from autogen_core import CancellationToken
from autogen_core.memory import (
    Memory,
    MemoryContent,
    MemoryMimeType,
    MemoryQueryResult,
    UpdateContextResult,
)
from autogen_core.model_context import ChatCompletionContext
from autogen_core.models import SystemMessage

from perseus_vault_client import VaultClient

logger = logging.getLogger(__name__)


class PerseusVaultMemory(Memory):
    """AutoGen ``Memory`` backed by a local Perseus Vault MCP server.

    Perseus Vault is a local-first persistent memory engine with structured entities,
    journal events, and state management. This adapter implements the four
    ``Memory`` protocol methods (``add``, ``query``, ``update_context``,
    ``clear``) plus ``close`` so it can be passed directly to an
    ``AssistantAgent(memory=[...])``.
    """

    def __init__(
        self,
        binary: str = "perseus-vault",
        db_path: str = "~/.perseus-vault/data/perseus-vault.db",
        timeout: float = 30.0,
        category: str = "autogen",
        context_limit: int = 10,
        encryption_key: Optional[str] = None,
        llm_endpoint: Optional[str] = None,
        llm_model: Optional[str] = None,
    ):
        """Initialize the Perseus Vault-backed memory.

        Args:
            binary: Path to the perseus-vault binary (default: finds on PATH)
            db_path: Path to the Perseus Vault SQLite database
            timeout: Command timeout in seconds
            category: Default Perseus Vault category for stored memories
            context_limit: Max entities to inject in ``update_context``
            encryption_key: Optional path to AES-256-GCM key file
            llm_endpoint: Optional LLM endpoint (e.g. Ollama
                ``http://localhost:11434/api/generate``) for hybrid search
            llm_model: Optional model name used for embeddings / ``perseus_vault_ask``
        """
        self.binary = binary
        self.db_path = str(Path(db_path).expanduser())
        self.timeout = timeout
        self.category = category
        self.context_limit = context_limit
        self.encryption_key = encryption_key
        self.llm_endpoint = llm_endpoint
        self.llm_model = llm_model
        self._client: Optional[VaultClient] = None

    # ── session management ──────────────────────────────────────────

    def _get_client(self) -> VaultClient:
        """Lazily build the shared VaultClient (hardened stdio transport).

        The ``llm_endpoint`` / ``llm_model`` options map onto Perseus Vault
        CLI flags (``--llm-endpoint`` / ``--llm-model``) passed as extra serve
        args; the client itself owns process lifecycle, the handshake, timeouts,
        and reconnection.
        """
        if self._client is None:
            extra_args = []
            if self.llm_endpoint:
                extra_args += ["--llm-endpoint", self.llm_endpoint]
            if self.llm_model:
                extra_args += ["--llm-model", self.llm_model]
            self._client = VaultClient(
                binary=self.binary,
                db_path=self.db_path,
                encryption_key=self.encryption_key,
                timeout=self.timeout,
                extra_args=extra_args or None,
            )
        return self._client

    def _close_session(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def __del__(self):
        try:
            self._close_session()
        except Exception:
            pass

    # ── MCP call ────────────────────────────────────────────────────

    @staticmethod
    def _unwrap_result(result: dict) -> dict:
        """Unwrap an MCP ``tools/call`` result into Perseus Vault's payload dict.

        Perseus Vault returns the standard MCP envelope::

            {"content": [{"type": "text", "text": "<json>"}],
             "structuredContent": {...parsed json...}}

        The payload (``items``, ``context`` ...) lives in ``structuredContent``
        (preferred) or the JSON text of the first content block. Reading
        ``result["items"]`` directly always yields nothing.
        """
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text", "") if isinstance(content[0], dict) else ""
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _call_perseus_vault(self, method: str, params: dict) -> dict:
        """Call a Perseus Vault MCP tool via the shared VaultClient.

        The client spawns the process once and reuses it across calls (with
        hardened handshake, timeout-teardown, and auto-respawn). Returns the
        unwrapped Perseus Vault payload dict, identical to before.
        """
        try:
            client = self._get_client()
            result = client.call_tool_raw(method, params)
        except Exception as e:
            raise RuntimeError(f"Perseus Vault call failed ({method}): {e}")
        return self._unwrap_result(result)

    # ── AutoGen Memory protocol ─────────────────────────────────────

    async def add(
        self,
        content: MemoryContent,
        cancellation_token: CancellationToken | None = None,
    ) -> None:
        """Store a ``MemoryContent`` entry in Perseus Vault.

        Maps to ``perseus_vault_remember``. The content text becomes ``body_json``;
        ``metadata`` may carry an explicit ``category``/``key``, otherwise an
        auto key is generated.
        """
        metadata = content.metadata or {}
        category = metadata.get("category", self.category)
        key = metadata.get("key") or f"autogen-{int(time.time() * 1000)}"

        text = self._content_to_text(content)
        body = {"content": text, "mime_type": str(content.mime_type)}
        # Preserve any extra metadata keys (besides routing ones) in the body.
        for k, v in metadata.items():
            if k not in ("category", "key"):
                body[k] = v

        result = self._call_perseus_vault("perseus_vault_remember", {
            "category": category,
            "key": key,
            "body_json": json.dumps(body),
            "type": metadata.get("type", "autogen_memory"),
        })
        if "error" in result:
            logger.warning("PerseusVaultMemory.add failed: %s", result["error"])

    async def query(
        self,
        query: str | MemoryContent,
        cancellation_token: CancellationToken | None = None,
        **kwargs: Any,
    ) -> MemoryQueryResult:
        """Search stored memories via Perseus Vault FTS5 recall.

        Returns a ``MemoryQueryResult`` whose ``results`` is a list of
        ``MemoryContent`` reconstructed from Perseus Vault entities.
        """
        query_text = query if isinstance(query, str) else self._content_to_text(query)
        limit = int(kwargs.get("limit", self.context_limit))

        params: dict[str, Any] = {"query": query_text, "limit": limit}
        category = kwargs.get("category")
        if category:
            params["category"] = category

        result = self._call_perseus_vault("perseus_vault_recall", params)
        items = result.get("items", []) if "error" not in result else []

        results: list[MemoryContent] = []
        for item in items:
            body = item.get("body_json", "{}")
            try:
                parsed = json.loads(body)
                text = parsed.get("content", body)
            except (json.JSONDecodeError, TypeError):
                text = body
            results.append(MemoryContent(
                content=text,
                mime_type=MemoryMimeType.TEXT,
                metadata={
                    "category": item.get("category", ""),
                    "key": item.get("key", ""),
                    "score": item.get("decay_score"),
                },
            ))

        return MemoryQueryResult(results=results)

    async def update_context(
        self,
        model_context: ChatCompletionContext,
    ) -> UpdateContextResult:
        """Inject Perseus Vault's context block into the model context.

        Calls ``perseus_vault_context`` and prepends the rendered markdown block as a
        ``SystemMessage`` so the agent starts each turn with its persistent
        memory loaded. Returns the memories used for transparency/telemetry.
        """
        result = self._call_perseus_vault("perseus_vault_context", {"limit": self.context_limit})
        if "error" in result:
            logger.warning("PerseusVaultMemory.update_context failed: %s", result["error"])
            return UpdateContextResult(memories=MemoryQueryResult(results=[]))

        context_text = result.get("context", "")
        if not context_text:
            return UpdateContextResult(memories=MemoryQueryResult(results=[]))

        await model_context.add_message(
            SystemMessage(content=f"Relevant memory context from Perseus Vault:\n{context_text}")
        )

        memory = MemoryContent(content=context_text, mime_type=MemoryMimeType.TEXT)
        return UpdateContextResult(memories=MemoryQueryResult(results=[memory]))

    async def clear(self) -> None:
        """Clear stored memories for this memory's category.

        Maps to ``perseus_vault_prune`` scoped to the configured category. This is a
        soft-delete (archived=1) — entities are recoverable, not destroyed.
        """
        result = self._call_perseus_vault("perseus_vault_prune", {"category": self.category})
        if "error" in result:
            logger.warning("PerseusVaultMemory.clear failed: %s", result["error"])

    async def close(self) -> None:
        """Shut down the persistent Perseus Vault process."""
        self._close_session()

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _content_to_text(content: MemoryContent) -> str:
        """Coerce a ``MemoryContent.content`` (str | bytes | dict | ...) to text."""
        c = content.content
        if isinstance(c, str):
            return c
        if isinstance(c, bytes):
            try:
                return c.decode("utf-8", errors="replace")
            except Exception:
                return str(c)
        try:
            return json.dumps(c)
        except (TypeError, ValueError):
            return str(c)


# Convenience helper
def create_perseus_vault_memory(
    db_path: str = "~/.perseus-vault/data/perseus-vault.db",
    **kwargs,
) -> PerseusVaultMemory:
    """Create a PerseusVaultMemory with sensible defaults.

    Args:
        db_path: Path to the Perseus Vault database
        **kwargs: Additional PerseusVaultMemory arguments
    """
    return PerseusVaultMemory(db_path=db_path, **kwargs)
