"""
CrewAI Perseus Vault Memory Tool — provides persistent memory for CrewAI agents.

Usage:
    from crewai import Agent, Task, Crew
    from perseus_vault_crewai import PerseusVaultMemoryTool

    memory = PerseusVaultMemoryTool(db_path="~/.perseus-vault/data/crew.db")

    agent = Agent(
        role="Researcher",
        goal="Find information",
        tools=[memory],
    )
"""

import json
import time
from pathlib import Path
from typing import Optional, Any
from crewai.tools import BaseTool

from perseus_vault_client import VaultClient


class PerseusVaultMemoryTool(BaseTool):
    """CrewAI tool that provides persistent memory via Perseus Vault.

    Agents can remember facts, recall past decisions, and search
    the knowledge base — all persisted across sessions and crews.

    Available actions:
        remember  — Store a fact or decision
        recall    — Search stored memories
        journal   — Record a significant event
        context   — Get the current session context summary

    The tool keeps a persistent Perseus Vault stdio session — the process is
    spawned once and reused across all calls.  This avoids the per-call
    cold-start overhead (process spawn + DB open + init handshake).
    """

    name: str = "Perseus Vault Memory"
    description: str = (
        "Persistent memory tool for storing and retrieving information "
        "across sessions. Use this to remember facts, recall past "
        "decisions, and maintain context between agent interactions.\n"
        "Actions: remember(category, key, content), "
        "recall(query, category?), "
        "journal(event_type, description, context?), "
        "context() — get session summary"
    )

    def __init__(
        self,
        binary: str = "perseus-vault",
        db_path: str = "~/.perseus-vault/data/crew.db",
        timeout: float = 30.0,
        encryption_key: Optional[str] = None,
    ):
        super().__init__()
        self.binary = binary
        self.db_path = str(Path(db_path).expanduser())
        self.timeout = timeout
        self.encryption_key = encryption_key
        self._client: Optional[VaultClient] = None

    # ── session management ──────────────────────────────────────────

    def _get_client(self) -> VaultClient:
        """Lazily build the shared VaultClient (hardened stdio transport)."""
        if self._client is None:
            self._client = VaultClient(
                binary=self.binary,
                db_path=self.db_path,
                encryption_key=self.encryption_key,
                timeout=self.timeout,
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

    # ── CrewAI tool interface ───────────────────────────────────────

    def _run(self, action: str, **kwargs) -> str:
        """Execute a memory action.

        Args:
            action: One of 'remember', 'recall', 'journal', 'context'
            **kwargs: Action-specific parameters
        """
        if action == "remember":
            return self._remember(**kwargs)
        elif action == "recall":
            return self._recall(**kwargs)
        elif action == "journal":
            return self._journal(**kwargs)
        elif action == "context":
            return self._context()
        else:
            return f"Unknown action: {action}. Use: remember, recall, journal, context"

    def _remember(
        self,
        category: str = "crewai",
        key: str = "",
        content: str = "",
        entity_type: str = "fact",
    ) -> str:
        """Store a fact, decision, or piece of knowledge."""
        result = self._call_perseus_vault("perseus_vault_remember", {
            "category": category,
            "key": key or f"auto-{int(time.time())}",
            "body_json": json.dumps({"content": content}),
            "type": entity_type,
        })
        if "error" in result:
            return f"Failed to remember: {result['error']}"
        return f"Remembered: [{category}] {key or 'auto'}: {content[:100]}"

    def _recall(
        self,
        query: str = "",
        category: str = "",
        limit: int = 5,
    ) -> str:
        """Search stored memories."""
        params = {"query": query, "limit": limit}
        if category:
            params["category"] = category

        result = self._call_perseus_vault("perseus_vault_recall", params)
        items = result.get("items", [])

        if not items:
            return f"No memories found for '{query}'"

        lines = [f"Found {len(items)} memor{'y' if len(items)==1 else 'ies'}:"]
        for item in items:
            body = item.get("body_json", "{}")
            try:
                content = json.loads(body).get("content", body)
            except (json.JSONDecodeError, TypeError):
                content = body
            lines.append(f"  [{item.get('category', '?')}] {item.get('key', '?')}: {content[:200]}")
        return "\n".join(lines)

    def _journal(
        self,
        event_type: str = "observation",
        description: str = "",
        context: str = "",
    ) -> str:
        """Record a significant event in the journal."""
        result = self._call_perseus_vault("perseus_vault_journal", {
            "event_type": event_type,
            "category": "crewai",
            "key": f"event-{int(time.time())}",
            "evaluated": {"description": description, "context": context},
        })
        if "error" in result:
            return f"Failed to journal: {result['error']}"
        return f"Journaled {event_type}: {description[:100]}"

    def _context(self) -> str:
        """Get a summary of recent memories for session context."""
        result = self._call_perseus_vault("perseus_vault_context", {})
        if "error" in result:
            return f"Failed to get context: {result['error']}"
        context_text = result.get("context", "")
        if not context_text:
            return "No stored context. Use 'remember' to store information first."
        return context_text[:1000] + ("..." if len(context_text) > 1000 else "")
