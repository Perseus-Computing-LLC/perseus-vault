"""Perseus Vault adapter for PraisonAI (#551).

A drop-in :class:`praisonaiagents.memory.protocols.MemoryProtocol` backend that
routes PraisonAI's short-term / long-term memory onto a local ``perseus-vault``
MCP server. Perseus Vault is a single static binary (SQLite + FTS5 + bundled
ONNX embeddings, optional AES-256-GCM), so PraisonAI agents get persistent,
local-first, offline memory with no external service.

The transport is the shared, hardened :class:`perseus_vault_client.VaultClient`
(spawn-once stdio session, deadline-bounded reads, auto-respawn) — the same
seam the LangGraph / CrewAI / AutoGen adapters use, so the stdio contract is
maintained in exactly one place.

Registration
------------
Importing this module self-registers the adapter under the name
``"perseus_vault"`` when PraisonAI is installed::

    import perseus_vault_praison  # registers "perseus_vault"
    from praisonaiagents.memory.adapters import get_memory_adapter

    mem = get_memory_adapter("perseus_vault", db_path="./agent.db")

Or use it directly as a :class:`MemoryProtocol` implementation::

    from perseus_vault_praison import PerseusVaultAdapter

    mem = PerseusVaultAdapter(db_path="./agent.db")
    mem.store_long_term("User prefers metric units.", {"source": "chat"})
    hits = mem.search_long_term("units preference")

Protocol coverage
-----------------
Implements the full ``MemoryProtocol`` plus the ``ResettableMemoryProtocol`` and
``DeletableMemoryProtocol`` extensions:

- ``store_short_term`` / ``search_short_term``
- ``store_long_term``  / ``search_long_term``
- ``get_all_memories`` (paginated enumeration across both categories — no
  truncation, via the shared client's ``scan``)
- ``get_context`` (hybrid recall assembled into a context string)
- ``reset_short_term`` / ``reset_long_term``
- ``delete_memory`` / ``delete_memories``
- ``get_entity`` / ``save_session``

Short-term and long-term memories live in two dedicated Perseus Vault
categories so they can be searched, enumerated, and reset independently.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from perseus_vault_client import VaultClient

__all__ = ["PerseusVaultAdapter", "register"]

__version__ = "0.1.0"

# Adapter name in the PraisonAI registry.
ADAPTER_NAME = "perseus_vault"

# Dedicated categories keep short/long-term searchable + resettable in isolation.
SHORT_TERM_CATEGORY = "praison_short_term"
LONG_TERM_CATEGORY = "praison_long_term"


class PerseusVaultAdapter:
    """PraisonAI ``MemoryProtocol`` backed by a local Perseus Vault MCP server.

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
    timeout:
        Per-request deadline in seconds (default 30).
    env:
        Extra environment variables for the vault subprocess.

    Extra keyword arguments are accepted and ignored so the adapter can be
    constructed by PraisonAI's registry with an arbitrary ``config`` blob.
    """

    def __init__(
        self,
        binary: Optional[str] = None,
        db_path: Optional[str] = None,
        *,
        encryption_key: Optional[str] = None,
        timeout: float = 30.0,
        env: Optional[Dict[str, str]] = None,
        **_ignored: Any,
    ) -> None:
        self._binary = binary or os.getenv("PERSEUS_VAULT_BIN", "perseus-vault")
        self._db_path = db_path or os.getenv("PERSEUS_VAULT_DB", "./perseus-vault.db")
        self._encryption_key = encryption_key or os.getenv("PERSEUS_VAULT_ENCRYPTION_KEY")
        self._timeout = float(timeout)
        self._env = env
        self._client: Optional[VaultClient] = None

    # -- lifecycle ----------------------------------------------------------

    def _get_client(self) -> VaultClient:
        if self._client is None:
            self._client = VaultClient(
                binary=self._binary,
                db_path=self._db_path,
                encryption_key=self._encryption_key,
                timeout=self._timeout,
                env=self._env,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __del__(self):  # best-effort
        try:
            self.close()
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _new_key(prefix: str) -> str:
        # Millisecond time + short uuid: sortable and collision-resistant, so
        # multiple stores in the same second never key-collide (a bug the
        # date-only harvest key hit — see perseus-vault #563).
        return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def _store(self, category: str, text: str, metadata: Optional[Dict[str, Any]]) -> str:
        """Store one memory; returns the entity key as the memory id."""
        key = self._new_key(category)
        body: Dict[str, Any] = {"content": text}
        if metadata:
            body["metadata"] = metadata
        res = self._get_client().remember(category, key, body)
        # The vault echoes the key back (and may dedup to an existing one).
        if isinstance(res, dict):
            return str(res.get("key", key))
        return key

    def _search(
        self, category: str, query: str, limit: int, **extra: Any
    ) -> List[Dict[str, Any]]:
        """Search one category, returning PraisonAI-shaped entries."""
        # Hybrid recall (keyword + vector) is the best general default; empty
        # query enumerates the category (the vault's documented match-all path).
        mode = extra.pop("mode", "hybrid" if query.strip() else "fts5")
        items = self._get_client().recall(
            query, category=category, limit=limit, mode=mode, **extra
        )
        return [self._to_entry(category, it) for it in items]

    @staticmethod
    def _to_entry(category: str, item: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a shared-client item into the PraisonAI memory shape
        (``{id, text, type, metadata, score}``)."""
        mem_type = "short" if category == SHORT_TERM_CATEGORY else "long"
        return {
            "id": item.get("id"),
            "text": item.get("text", ""),
            "type": mem_type,
            "metadata": item.get("metadata") or {},
            "score": item.get("score", 0.0),
        }

    # -- MemoryProtocol -----------------------------------------------------

    def store_short_term(
        self, text: str, metadata: Optional[Dict[str, Any]] = None, **kwargs
    ) -> str:
        return self._store(SHORT_TERM_CATEGORY, text, metadata)

    def search_short_term(
        self, query: str, limit: int = 5, **kwargs
    ) -> List[Dict[str, Any]]:
        return self._search(SHORT_TERM_CATEGORY, query, limit, **kwargs)

    def store_long_term(
        self, text: str, metadata: Optional[Dict[str, Any]] = None, **kwargs
    ) -> str:
        return self._store(LONG_TERM_CATEGORY, text, metadata)

    def search_long_term(
        self, query: str, limit: int = 5, **kwargs
    ) -> List[Dict[str, Any]]:
        return self._search(LONG_TERM_CATEGORY, query, limit, **kwargs)

    def get_all_memories(self, **kwargs) -> List[Dict[str, Any]]:
        """Enumerate every short- and long-term memory.

        Uses the shared client's paginated ``scan`` so the result is the WHOLE
        category across as many pages as needed — not a single truncated page
        (the 100-cap footgun called out in perseus-vault #562).
        """
        client = self._get_client()
        out: List[Dict[str, Any]] = []
        for category in (SHORT_TERM_CATEGORY, LONG_TERM_CATEGORY):
            for it in client.scan(category, page_size=kwargs.get("page_size", 100)):
                out.append(self._to_entry(category, it))
        return out

    def get_context(self, query: str = "", limit: int = 5, **kwargs) -> str:
        """Assemble a context string from the most relevant long-term memories.

        Maps onto hybrid recall (``get_context`` in PraisonAI is used to inject
        relevant prior knowledge into a prompt).
        """
        hits = self.search_long_term(query, limit=limit, **kwargs) if query else \
            [self._to_entry(LONG_TERM_CATEGORY, it)
             for it in self._get_client().scan(LONG_TERM_CATEGORY, max_items=limit)]
        lines = [h["text"] for h in hits if h.get("text")]
        return "\n\n".join(lines)

    # -- ResettableMemoryProtocol ------------------------------------------

    def reset_short_term(self) -> None:
        self._get_client().prune(SHORT_TERM_CATEGORY, purge_all=True)

    def reset_long_term(self) -> None:
        self._get_client().prune(LONG_TERM_CATEGORY, purge_all=True)

    # -- DeletableMemoryProtocol -------------------------------------------

    def delete_memory(self, memory_id: str, memory_type: Optional[str] = None) -> bool:
        """Delete a memory by its id (the entity key).

        ``memory_type`` ('short'/'long') narrows which category is tried; when
        omitted both are attempted. Returns True if the vault archived a row.
        """
        client = self._get_client()
        categories: List[str]
        if memory_type == "short":
            categories = [SHORT_TERM_CATEGORY]
        elif memory_type == "long":
            categories = [LONG_TERM_CATEGORY]
        else:
            categories = [SHORT_TERM_CATEGORY, LONG_TERM_CATEGORY]
        for category in categories:
            if client.forget(category, memory_id):
                return True
        return False

    def delete_memories(self, memory_ids: List[str]) -> int:
        return sum(1 for mid in memory_ids if self.delete_memory(mid))

    # -- convenience beyond the core protocol ------------------------------

    def get_entity(self, entity_id: str) -> Dict[str, Any]:
        """Fetch a single stored entity by id with its full body."""
        return self._get_client().get_entity(entity_id)

    def save_session(self, session_id: str, data: Dict[str, Any], **kwargs) -> str:
        """Persist a session blob under a dedicated ``session`` category."""
        res = self._get_client().remember(
            "session", session_id, data if isinstance(data, dict) else {"data": data}
        )
        return str(res.get("key", session_id)) if isinstance(res, dict) else session_id


def register(name: str = ADAPTER_NAME) -> bool:
    """Register :class:`PerseusVaultAdapter` in PraisonAI's memory registry.

    Returns True on success, False when PraisonAI is not installed (so importing
    this package is always safe even without PraisonAI present).
    """
    try:
        from praisonaiagents.memory.adapters import register_memory_adapter
    except Exception:
        return False
    register_memory_adapter(name, PerseusVaultAdapter)
    return True


# Self-register on import when PraisonAI is available. Import stays a no-op
# (no error) when it is not, so the package is usable standalone too.
register()
