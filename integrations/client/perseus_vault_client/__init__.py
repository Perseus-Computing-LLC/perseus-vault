"""Perseus Vault — official Python client.

A small, dependency-free client for driving a local ``perseus-vault`` binary
over its MCP JSON-RPC 2.0 stdio transport (``perseus-vault serve``).

This exists so that framework integrations (LangGraph, CrewAI, AutoGen,
PraisonAI, pydantic-ai, …) don't each re-implement — and re-break — the stdio
transport. The tricky parts are centralized and hardened here once:

- **Reentrant-lock handshake.** ``initialize`` runs inside ``_request`` which
  itself needs the lock, so a non-reentrant lock would deadlock.
- **Spawn under the lock.** Prevents a concurrent-startup race that would leak
  multiple child processes.
- **Deadline-bounded reads with teardown.** A plain ``readline()`` blocks
  forever if the child accepts stdin but never emits a newline. Reads happen on
  a daemon thread against a deadline; on timeout the child is terminated so a
  later call never races a still-blocked reader on a reused stdout.
- **Auto-respawn.** If the child has died, the next call starts a fresh one.
- **Normalized results.** ``call_tool`` unwraps the MCP ``content`` envelope and
  parses JSON bodies; recall-style helpers return uniform dicts.

The client is transport-only and knows nothing about any framework. Typed
convenience methods are provided for the common tools; anything else is
reachable via :meth:`VaultClient.call_tool`.

Example
-------
>>> from perseus_vault_client import VaultClient
>>> with VaultClient(binary="perseus-vault", db_path="./vault.db") as vault:
...     vault.remember("architecture", "use-sqlite", {"content": "SQLite + FTS5"})
...     hits = vault.recall("database choice", limit=3)
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = ["VaultClient", "VaultError", "VaultTimeoutError"]

__version__ = "0.1.0"

# Default protocol version advertised in the MCP handshake.
_PROTOCOL_VERSION = "2024-11-05"


class VaultError(RuntimeError):
    """A Perseus Vault MCP call returned an error or the transport failed."""


class VaultTimeoutError(VaultError, TimeoutError):
    """The vault process did not respond within the configured timeout."""


class VaultClient:
    """Client for a local ``perseus-vault`` MCP stdio server.

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
        Per-request deadline in seconds (default 30). A request that exceeds it
        raises :class:`VaultTimeoutError` and the child process is torn down.
    env:
        Extra environment variables for the child process.
    tool_prefix:
        Canonical tool namespace (default ``"perseus_vault"``). The helper
        methods call ``f"{tool_prefix}_{tool}"``.
    """

    def __init__(
        self,
        binary: Optional[str] = None,
        db_path: Optional[str] = None,
        *,
        encryption_key: Optional[str] = None,
        timeout: float = 30.0,
        env: Optional[Dict[str, str]] = None,
        tool_prefix: str = "perseus_vault",
    ):
        self._binary = binary or os.getenv("PERSEUS_VAULT_BIN", "perseus-vault")
        self._db_path = db_path or os.getenv("PERSEUS_VAULT_DB", "./perseus-vault.db")
        self._encryption_key = encryption_key or os.getenv("PERSEUS_VAULT_ENCRYPTION_KEY")
        self._timeout = float(timeout)
        self._env = {**os.environ, **(env or {})}
        self._prefix = tool_prefix

        # Reentrant: _request recurses into _start -> _request during the
        # handshake while already holding the lock.
        self._lock = threading.RLock()
        self._id = 0
        self._proc: Optional[subprocess.Popen] = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> "VaultClient":
        self._ensure_started()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):  # best-effort
        try:
            self.close()
        except Exception:
            pass

    def _ensure_started(self) -> None:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()

    def _start(self) -> None:
        cmd = [self._binary, "serve", "--db", self._db_path]
        if self._encryption_key:
            cmd += ["--encryption-key", self._encryption_key]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self._env,
            )
        except FileNotFoundError as exc:
            raise VaultError(
                f"Could not launch perseus-vault binary {self._binary!r}. "
                "Install it (single static binary, no deps) from "
                "https://github.com/Perseus-Computing-LLC/perseus-vault and put "
                "it on PATH or pass binary=/path/to/perseus-vault."
            ) from exc
        # Handshake.
        self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "perseus-vault-client", "version": __version__},
        })
        self._notify("notifications/initialized", {})

    def _teardown(self) -> None:
        """Terminate the child. Used on close and after a timeout, where a
        daemon reader thread may still be attached to the old stdout — killing
        the process unblocks it and guarantees the next request respawns clean.
        """
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def close(self) -> None:
        with self._lock:
            self._teardown()

    # -- transport ----------------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _readline_with_timeout(self, timeout: float) -> Optional[str]:
        """Read one stdout line, giving up after ``timeout`` seconds.

        Returns the line, ``""`` on EOF, or ``None`` on timeout. The read runs
        on a daemon thread so a hung child cannot block forever.
        """
        assert self._proc and self._proc.stdout
        result: List[Optional[str]] = [None]

        def _read() -> None:
            try:
                result[0] = self._proc.stdout.readline()
            except Exception:
                result[0] = None

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None
        return result[0]

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            rid = self._next_id()
            msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
            deadline = time.time() + self._timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self._teardown()
                    raise VaultTimeoutError(
                        f"perseus-vault did not respond to {method} in {self._timeout}s"
                    )
                line = self._readline_with_timeout(remaining)
                if line is None:
                    # Timed out mid-read: reader thread is still blocked on this
                    # stdout, so tear the child down rather than reuse it.
                    self._teardown()
                    raise VaultTimeoutError(
                        f"perseus-vault did not respond to {method} in {self._timeout}s"
                    )
                if line == "":
                    raise VaultError("perseus-vault closed stdout unexpectedly")
                line = line.strip()
                if not line:
                    continue
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if resp.get("id") == rid:
                    if resp.get("error"):
                        raise VaultError(f"perseus-vault error: {resp['error']}")
                    return resp.get("result", {})

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        with self._lock:
            assert self._proc and self._proc.stdin
            self._proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
            self._proc.stdin.flush()

    # -- generic tool call --------------------------------------------------

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Invoke an MCP tool by name and return its payload.

        Perseus Vault returns the standard MCP envelope with both a parsed
        ``structuredContent`` object and a ``content`` text block. We prefer
        ``structuredContent`` (no re-parse), fall back to JSON-decoding the
        first text block, and finally return the raw result dict.
        """
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content", [])
        if not content:
            return result
        text = content[0].get("text", "") if isinstance(content[0], dict) else ""
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def call_tool_raw(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Invoke an MCP tool and return the full unwrapped ``result`` envelope
        (both ``content`` and ``structuredContent``). Use when you need the
        envelope rather than just the payload."""
        return self._request("tools/call", {"name": name, "arguments": arguments})

    def list_tools(self) -> List[str]:
        """Return the advertised tool names."""
        result = self._request("tools/list", {})
        return [t["name"] for t in result.get("tools", [])]

    def _tool(self, short: str) -> str:
        return f"{self._prefix}_{short}"

    # -- typed convenience helpers -----------------------------------------

    def remember(
        self,
        category: str,
        key: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
        *,
        importance: Optional[float] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Store or update an entity. Returns the vault's result dict.

        ``key`` is generated if omitted. ``body`` is the entity body (stored as
        ``body_json``); extra kwargs pass through to the tool (tags, type, …).
        """
        key = key or f"{category}-{uuid.uuid4().hex[:12]}"
        args: Dict[str, Any] = {
            "category": category,
            "key": key,
            "body_json": json.dumps(body or {}),
        }
        if importance is not None:
            args["importance"] = importance
        args.update(extra)
        res = self.call_tool(self._tool("remember"), args)
        if isinstance(res, dict):
            res.setdefault("key", key)
            return res
        return {"key": key, "result": res}

    def recall(
        self,
        query: str,
        *,
        category: Optional[str] = None,
        limit: int = 10,
        mode: str = "hybrid",
        offset: Optional[int] = None,
        **extra: Any,
    ) -> List[Dict[str, Any]]:
        """Keyword/hybrid search. Returns a list of normalized item dicts
        ``{id, text, metadata, score, raw}``. An empty ``query`` enumerates the
        category (ordered by the vault's ranking)."""
        args: Dict[str, Any] = {"query": query, "limit": limit, "mode": mode}
        if category is not None:
            args["category"] = category
        if offset is not None:
            args["offset"] = offset
        args.update(extra)
        res = self.call_tool(self._tool("recall"), args)
        return self._normalize_items(res)

    def semantic_search(
        self, query: str, *, category: Optional[str] = None, limit: int = 10, **extra: Any
    ) -> List[Dict[str, Any]]:
        """Dense-only semantic search. Same normalized item shape as :meth:`recall`."""
        args: Dict[str, Any] = {"query": query, "limit": limit}
        if category is not None:
            args["category"] = category
        args.update(extra)
        return self._normalize_items(self.call_tool(self._tool("semantic_search"), args))

    def scan(
        self, category: str, *, page_size: int = 100, max_items: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Enumerate every entity in a category.

        Prefers the server-side ``scan`` tool (#562): keyset pages ordered by
        immutable entity id with a continuation cursor, so the walk is
        deterministic (recall reinforcement cannot skip/repeat rows), free of
        recall's offset cap, and side-effect-free. Falls back to legacy
        offset-paged empty-query recall on servers that predate the tool.
        """
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            args: Dict[str, Any] = {"category": category, "limit": page_size}
            if cursor:
                args["cursor"] = cursor
            res = self.call_tool(self._tool("scan"), args)
            if not isinstance(res, dict) or "items" not in res:
                # Pre-#562 server: no scan tool. Legacy offset paging.
                return self._scan_via_recall_offset(
                    category, page_size=page_size, max_items=max_items
                )
            out.extend(self._normalize_items(res))
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]
            cursor = res.get("next_cursor")
            if not res.get("has_more") or not cursor:
                break
        return out

    def _scan_via_recall_offset(
        self, category: str, *, page_size: int = 100, max_items: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Legacy enumeration for servers without the ``scan`` tool: page
        empty-query fts5 recall with ``offset`` until a short page. Subject to
        recall's ordering side-effects and offset cap; upgrade the server for
        the deterministic path."""
        out: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = self.recall("", category=category, limit=page_size, mode="fts5", offset=offset)
            if not page:
                break
            out.extend(page)
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]
            if len(page) < page_size:
                break
            offset += page_size
        return out

    def context(self, query: Optional[str] = None, **extra: Any) -> str:
        """Return the vault's pre-rendered markdown context block for injection."""
        args: Dict[str, Any] = {}
        if query:
            args["query"] = query
        args.update(extra)
        res = self.call_tool(self._tool("context"), args)
        if isinstance(res, str):
            return res
        if isinstance(res, dict):
            return res.get("markdown") or res.get("context") or res.get("text") or ""
        return ""

    def forget(self, category: str, key: str, *, reason: Optional[str] = None) -> bool:
        """Soft-delete an entity. Returns True only if the vault archived it."""
        args: Dict[str, Any] = {"category": category, "key": key}
        if reason:
            args["reason"] = reason
        res = self.call_tool(self._tool("forget"), args)
        return bool(isinstance(res, dict) and res.get("archived", 0))

    def prune(self, category: str, *, purge_all: bool = False, **extra: Any) -> Dict[str, Any]:
        """Bulk-archive entities in a category. ``purge_all=True`` clears the
        whole category (leaving other categories untouched)."""
        args: Dict[str, Any] = {"category": category}
        if purge_all:
            args["purge_all"] = True
        args.update(extra)
        res = self.call_tool(self._tool("prune"), args)
        return res if isinstance(res, dict) else {"result": res}

    def get_entity(self, entity_id: str) -> Dict[str, Any]:
        """Fetch a single entity by id with its full body."""
        res = self.call_tool(self._tool("get_entity"), {"id": entity_id})
        return res if isinstance(res, dict) else {"result": res}

    def stats(self) -> Dict[str, Any]:
        res = self.call_tool(self._tool("stats"), {})
        return res if isinstance(res, dict) else {"result": res}

    def health(self) -> Dict[str, Any]:
        res = self.call_tool(self._tool("health"), {})
        return res if isinstance(res, dict) else {"result": res}

    # -- normalization ------------------------------------------------------

    @staticmethod
    def _normalize_items(res: Any) -> List[Dict[str, Any]]:
        items = res.get("items", []) if isinstance(res, dict) else []
        out: List[Dict[str, Any]] = []
        for it in items:
            body = it.get("body_json") or it.get("body") or {}
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    body = {"content": body}
            score = it.get("score")
            if score is None:
                score = it.get("confidence")
            out.append({
                "id": it.get("key") or it.get("id"),
                "text": (body or {}).get("content", "") if isinstance(body, dict) else "",
                "metadata": (body or {}).get("metadata") or {} if isinstance(body, dict) else {},
                "score": score if isinstance(score, (int, float)) else 0.0,
                "raw": it,
            })
        return out
