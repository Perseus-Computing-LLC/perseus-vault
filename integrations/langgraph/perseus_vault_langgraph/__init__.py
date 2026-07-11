"""
PerseusVaultStore — LangGraph BaseStore implementation backed by Perseus Vault.

Drop-in persistent long-term memory for LangGraph agents.
Maps LangGraph's namespace/key/value model to Perseus Vault's entity model.

Usage:
    from perseus_vault_langgraph import PerseusVaultStore
    from langgraph.store.memory import InMemoryStore

    store = PerseusVaultStore()  # connects to local Perseus Vault via MCP stdio
    # Or with explicit config:
    store = PerseusVaultStore(
        binary="/usr/local/bin/perseus-vault",
        db_path="~/.perseus-vault/data/perseus-vault.db"
    )

    # Use as any BaseStore
    store.put(("users", "123"), "prefs", {"theme": "dark"})
    item = store.get(("users", "123"), "prefs")
    results = store.search(("users",), query="preferences")
"""

from __future__ import annotations

import json
import time
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from langgraph.store.base import BaseStore, Item, SearchItem, Op, Result

from perseus_vault_client import VaultClient

# The "no TTL given" sentinel was renamed NOT_GIVEN -> NOT_PROVIDED in
# LangGraph 1.0. Support both so the adapter imports across versions.
try:
    from langgraph.store.base import NOT_PROVIDED as _NOT_GIVEN
except ImportError:  # langgraph < 1.0
    from langgraph.store.base import NOT_GIVEN as _NOT_GIVEN

logger = logging.getLogger(__name__)


class PerseusVaultStore(BaseStore):
    """LangGraph BaseStore backed by a local Perseus Vault MCP server.

    Perseus Vault is a local-first persistent memory engine with structured entities,
    journal events, and state management. This adapter maps LangGraph's
    namespace/key/value model onto Perseus Vault's entity model.

    Mapping:
        namespace tuple  → Perseus Vault category (joined with '/')
        key              → Perseus Vault key
        value dict       → Perseus Vault body_json
        search query     → Perseus Vault FTS5 recall
    """

    def __init__(
        self,
        binary: str = "perseus-vault",
        db_path: str = "~/.perseus-vault/data/perseus-vault.db",
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        encryption_key: Optional[str] = None,
        ollama_url: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ):
        """Initialize the Perseus Vault-backed store.

        Args:
            binary: Path to the perseus-vault binary (default: finds on PATH)
            db_path: Path to the Perseus Vault SQLite database
            timeout: Command timeout in seconds
            connect_timeout: MCP handshake timeout in seconds
            encryption_key: Optional path to AES-256-GCM key file
            ollama_url: Optional Ollama endpoint for hybrid search
            embedding_model: Optional embedding model name (requires ollama_url)
        """
        self.binary = binary
        self.db_path = str(Path(db_path).expanduser())
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.encryption_key = encryption_key
        self.ollama_url = ollama_url
        self.embedding_model = embedding_model
        self._client: Optional[VaultClient] = None

    def _get_client(self) -> VaultClient:
        """Lazily build the shared VaultClient (hardened stdio transport).

        The ``ollama_url`` / ``embedding_model`` options map onto Perseus Vault
        CLI flags (``--llm-endpoint`` / ``--llm-model``) passed as extra serve
        args; the client itself owns process lifecycle, the handshake, timeouts,
        and reconnection.
        """
        if self._client is None:
            extra_args = []
            if self.ollama_url:
                # maps to --llm-endpoint (there is no --ollama-url flag)
                extra_args += ["--llm-endpoint", self.ollama_url]
            if self.embedding_model:
                # maps to --llm-model for Ollama-backed embeddings
                # (--embedding-model expects an ONNX model *path*, not a name)
                extra_args += ["--llm-model", self.embedding_model]
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

    def _namespace_to_category(self, namespace: tuple[str, ...]) -> str:
        """Convert LangGraph namespace tuple to Perseus Vault category string."""
        return "/".join(namespace) if namespace else "default"

    def _category_to_namespace(self, category: str) -> tuple[str, ...]:
        """Convert Perseus Vault category string back to namespace tuple."""
        return tuple(category.split("/")) if category != "default" else ()

    @staticmethod
    def _unwrap_result(result: dict) -> dict:
        """Unwrap an MCP ``tools/call`` result into Perseus Vault's payload dict.

        Perseus Vault returns the standard MCP envelope::

            {"content": [{"type": "text", "text": "<json>"}],
             "structuredContent": {...parsed json...}}

        The actual payload (``items``, ``by_category``, ``context`` ...) lives
        in ``structuredContent`` (preferred) or, failing that, in the JSON text
        of the first content block. Reading ``result["items"]`` directly always
        yields nothing, so callers must go through this helper.
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

    @staticmethod
    def _ms_to_dt(ms: Any) -> datetime:
        """Convert a Perseus Vault ``*_unix_ms`` timestamp to a UTC ``datetime``.

        ``Item.created_at`` / ``updated_at`` are typed ``datetime``; the epoch
        is used as a fallback when a record carries no usable timestamp.
        """
        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        if not ms:
            return epoch
        try:
            return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return epoch

    def put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: list[str] | Literal[False] | None = None,  # type: ignore[name-defined]
        *,
        ttl: float | None | Any = _NOT_GIVEN,
    ) -> None:
        """Store a value in Perseus Vault.

        Maps to perseus_vault_remember with category=namespace, key=key.
        The value dict becomes body_json. TTL is stored as a state entry.
        """
        category = self._namespace_to_category(namespace)

        result = self._call_perseus_vault("perseus_vault_remember", {
            "category": category,
            "key": key,
            "body_json": json.dumps(value),
            "type": "langgraph_item",
        })

        # Handle TTL via Perseus Vault state
        if ttl is not _NOT_GIVEN and ttl is not None:
            self._call_perseus_vault("perseus_vault_state_set", {
                "key": f"{category}/{key}__ttl",
                "value": str(time.time() + float(ttl)),
                "ttl": float(ttl),
            })

    async def aput(self, *args, **kwargs) -> None:
        """Async variant — delegates to sync put."""
        self.put(*args, **kwargs)

    def get(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        refresh_ttl: bool | None = None,
    ) -> Item | None:
        """Retrieve a value from Perseus Vault.

        Maps to perseus_vault_recall filtered by category + key.
        """
        category = self._namespace_to_category(namespace)

        result = self._call_perseus_vault("perseus_vault_recall", {
            "query": key,
            "category": category,
            "limit": 5,
        })

        items = result.get("items", [])
        for item in items:
            if item.get("key") == key:
                try:
                    value = json.loads(item.get("body_json", "{}"))
                except (json.JSONDecodeError, TypeError):
                    value = {}

                return Item(
                    namespace=namespace,
                    key=key,
                    value=value,
                    created_at=self._ms_to_dt(item.get("created_at_unix_ms")),
                    updated_at=self._ms_to_dt(
                        item.get("last_accessed_unix_ms")
                        or item.get("created_at_unix_ms")
                    ),
                )

        return None

    async def aget(self, *args, **kwargs) -> Item | None:
        """Async variant — delegates to sync get."""
        return self.get(*args, **kwargs)

    def search(
        self,
        namespace_prefix: tuple[str, ...],
        /,
        *,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
        refresh_ttl: bool | None = None,
    ) -> list[SearchItem]:
        """Search for items in Perseus Vault.

        Uses Perseus Vault's FTS5 keyword search. The namespace_prefix becomes
        a category filter.
        """
        category = self._namespace_to_category(namespace_prefix)
        search_query = query or ""

        params = {
            "query": search_query,
            "limit": limit,
            "offset": offset,
        }
        if category and category != "default":
            params["category"] = category

        result = self._call_perseus_vault("perseus_vault_recall", params)
        items = result.get("items", [])

        results = []
        for item in items:
            try:
                value = json.loads(item.get("body_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                value = {}

            results.append(SearchItem(
                namespace=namespace_prefix,
                key=item.get("key", ""),
                value=value,
                created_at=self._ms_to_dt(item.get("created_at_unix_ms")),
                updated_at=self._ms_to_dt(
                    item.get("last_accessed_unix_ms")
                    or item.get("created_at_unix_ms")
                ),
                score=item.get("decay_score"),
            ))

        return results

    async def asearch(self, *args, **kwargs) -> list[SearchItem]:
        """Async variant — delegates to sync search."""
        return self.search(*args, **kwargs)

    def delete(self, namespace: tuple[str, ...], key: str) -> None:
        """Delete an item from Perseus Vault.

        Maps to perseus_vault_forget (soft-delete with archived=1).
        """
        category = self._namespace_to_category(namespace)
        self._call_perseus_vault("perseus_vault_forget", {
            "category": category,
            "key": key,
            "reason": "LangGraph delete",
        })

    async def adelete(self, *args, **kwargs) -> None:
        """Async variant — delegates to sync delete."""
        self.delete(*args, **kwargs)

    def list_namespaces(
        self,
        *,
        prefix: Any | None = None,
        suffix: Any | None = None,
        max_depth: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[str, ...]]:
        """List all namespaces (categories) in Perseus Vault."""
        result = self._call_perseus_vault("perseus_vault_stats", {})
        # perseus_vault_stats returns category counts under "by_category" (a mapping of
        # category name -> count), not a "categories" list.
        by_category = result.get("by_category", {})

        namespaces = []
        for cat in by_category:
            ns = self._category_to_namespace(cat)
            namespaces.append(ns)

        return namespaces[offset:offset + limit]

    async def alist_namespaces(self, *args, **kwargs) -> list[tuple[str, ...]]:
        """Async variant — delegates to sync list_namespaces."""
        return self.list_namespaces(*args, **kwargs)

    def batch(self, ops: Iterable[Op]) -> list[Result]:  # type: ignore[name-defined]
        """Execute a batch of operations."""
        results = []
        for op in ops:
            try:
                if op[0] == "put":
                    self.put(*op[1], **op[2] if len(op) > 2 else {})
                    results.append(None)
                elif op[0] == "delete":
                    self.delete(*op[1])
                    results.append(None)
                elif op[0] == "get":
                    results.append(self.get(*op[1], **op[2] if len(op) > 2 else {}))
                elif op[0] == "search":
                    results.append(self.search(*op[1], **op[2] if len(op) > 2 else {}))
                else:
                    results.append(None)
            except Exception as e:
                logger.error(f"Batch op {op[0]} failed: {e}")
                results.append(None)
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:  # type: ignore[name-defined]
        """Async variant — delegates to sync batch."""
        return self.batch(ops)


# Convenience helper
def create_perseus_vault_store(
    db_path: str = "~/.perseus-vault/data/perseus-vault.db",
    **kwargs,
) -> PerseusVaultStore:
    """Create a PerseusVaultStore with sensible defaults.

    Args:
        db_path: Path to the Perseus Vault database
        **kwargs: Additional PerseusVaultStore arguments
    """
    return PerseusVaultStore(db_path=db_path, **kwargs)
