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
import math
import os
import copy
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

__all__ = [
    "EphemeralAdmissionFixture",
    "VaultClient",
    "VaultError",
    "VaultTimeoutError",
]

__version__ = "0.1.0"

# Default protocol version advertised in the MCP handshake.
_PROTOCOL_VERSION = "2024-11-05"
_RECALL_WIRE_FIELDS = {
    "items", "total", "retrieval_profile", "variants", "diagnostic", "outcome", "gap", "gap_fill",
    "fused_trace", "conflict_flags", "abstain_hint", "conflict_flags_markdown",
    "freshness_summary", "freshness_gate", "evidence", "declared_graph",
}
_RECALL_OUTCOME_STATUS = {"fresh", "complete", "partial", "degraded", "empty", "unavailable", "timeout", "stale"}
_RECALL_OBJECT_FIELDS = {"diagnostic", "outcome", "fused_trace", "freshness_summary", "freshness_gate", "evidence", "declared_graph"}
_RECALL_STRING_FIELDS = {"gap_fill", "conflict_flags_markdown"}
_RECALL_BOOL_FIELDS = {"gap", "abstain_hint"}
_RECALL_LIST_FIELDS = {"conflict_flags"}
_RECALL_ITEM_FIELDS = frozenset({
    "key", "id", "body_json", "category", "score", "score_semantics", "decay_score", "why_served",
    "wire_rank", "created_at_unix_ms", "updated_at_unix_ms", "last_accessed_unix_ms",
})
_WHY_SERVED_FIELDS = frozenset({"reason", "memory_class"})
_RECALL_PROJECTION_ROOT_FIELDS = {
    "diagnostic": frozenset({"reason", "hint", "active_memories", "embedded_memories", "semantic_recall"}),
    "outcome": frozenset({"status", "abstained", "reason", "deadline_elapsed", "backend_health", "completeness", "candidate_scope"}),
    "fused_trace": frozenset({
        "original_query", "expansions", "strategies", "fusion", "truncation", "rerank", "placement",
        "state_filters", "sources", "graph_route", "validity", "anchor_matched", "multihop",
        "source_chain_exclusions", "selection_decisions",
    }),
    "freshness_summary": frozenset({"fresh", "expired", "never_verified"}),
    "freshness_gate": frozenset({"proceed", "verdict", "reason", "expired_ids", "note"}),
    "evidence": frozenset({"status", "lanes", "items", "budget", "excluded", "receipt"}),
    "declared_graph": frozenset({"schema_version", "workspace_hash", "source_key", "nodes", "edges", "truncated"}),
}
_RECALL_PROJECTION_FIELDS = frozenset({
    "reason", "hint", "active_memories", "embedded_memories", "semantic_recall", "status", "abstained",
    "deadline_elapsed", "backend_health", "completeness", "candidate_scope", "enabled",
    "query_embedding_available", "pending_embed_jobs", "scope", "degraded", "scanned", "total_embedded",
    "embedded_population", "pool_bound", "fresh", "expired", "never_verified", "proceed", "verdict",
    "expired_ids", "note", "schema_version", "workspace_hash", "source_key", "nodes", "edges", "truncated",
    "node_id", "namespace", "canonical_id", "node_type", "external_ref", "state", "edge_id", "manifest_id",
    "source_id", "source_key", "source_revision", "source_sha256", "manifest_span_ref", "from_node_id",
    "to_node_id", "from", "to", "predicate", "direction", "context", "source_span_ref", "origin",
    "attestation_state", "attested_by", "attestation_ref", "valid_from_unix_ms", "valid_to_unix_ms",
    "recorded_at_unix_ms", "lanes", "items", "budget", "excluded", "receipt", "lane", "entity_id", "source",
    "span", "source_groups", "chain_identity", "verification", "trust", "tokens", "revision", "span_sha256",
    "text", "start_char", "end_char", "max_tokens", "selected_tokens", "omitted_tokens", "per_lane",
    "selected_items", "omitted_items", "query_sha256", "requesting_agent_id", "as_of_unix_ms", "selected",
    "requirement_sha256", "candidate_set_sha256", "selected_set_sha256", "omitted_set_sha256", "reasons",
    "digest", "count", "candidate_id", "claim_id", "kind", "validity", "evidence_refs", "confidence",
    "disposition", "disclose_existence", "disclose_value", "invalidated_at_unix_ms", "original_query",
    "expansions", "strategies", "fusion", "truncation", "rerank", "placement", "state_filters", "sources",
    "graph_route", "anchor_matched", "multihop", "source_chain_exclusions", "strategy", "candidates", "top",
    "latency_ms", "rrf_k", "weights", "fused_count", "budget_tokens", "estimated_tokens_used", "retained",
    "dropped", "per_type", "profile", "allocations", "class", "floor", "cap", "floor_shortfall", "applied",
    "method", "utility", "selected", "skipped_reason", "unattested_edges_skipped", "out_of_scope_edges_skipped",
    "expired_targets_skipped", "dangling_targets_skipped", "flagged_context_invalid", "freshness_half_life_secs",
    "scope_bonus", "provenance_boost", "superseded_penalty", "expiring_penalty", "stale_freshness",
    "context_invalid_freshness", "grade_counts", "hop_expanded", "expanded_ids", "selection_order",
    "covered_entities", "uncovered_entities", "schema_version", "policy_digest", "arms", "candidate_count",
    "eligible_count", "retained_count", "delivered_count", "abstention_reason", "token_budget",
    "delivered_order", "replay_fingerprint_sha256", "arm", "source_chain_commitment", "source_chain_status",
    "source_arm_ranks", "fused_rank", "fused_score", "rerank_score", "validity_multiplier", "token_estimate",
    "token_estimator", "eligible", "final_rank",
})
_RECALL_PROJECTION_MAP_FIELDS = frozenset({"sources", "weights", "grade_counts", "source_arm_ranks"})


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
    extra_args:
        Additional arguments appended to the ``serve`` command without shell
        interpolation.
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
        extra_args: Optional[List[str]] = None,
        client_info_name: str = "perseus-vault-client",
    ):
        self._binary = binary or os.getenv("PERSEUS_VAULT_BIN", "perseus-vault")
        self._db_path = db_path or os.getenv("PERSEUS_VAULT_DB", "./perseus-vault.db")
        self._encryption_key = encryption_key or os.getenv("PERSEUS_VAULT_ENCRYPTION_KEY")
        self._timeout = float(timeout)
        self._env = {**os.environ, **(env or {})}
        self._prefix = tool_prefix
        if extra_args is not None and any(not isinstance(arg, str) for arg in extra_args):
            raise TypeError("extra_args must contain only strings")
        self._extra_args = list(extra_args or [])
        self._client_info_name = client_info_name

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
        cmd = [self._binary, "serve", "--db", self._db_path, *self._extra_args]
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
            "clientInfo": {"name": self._client_info_name, "version": __version__},
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
        ``{id, text, metadata, score, score_semantics, wire_rank, raw}``.
        ``score`` is ``None`` when the server did not provide an explicit
        semantic relevance score; ``decay_score`` is never substituted. An
        empty ``query`` enumerates the category (ordered by the vault's ranking)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise VaultError("recall response unavailable: malformed limit")
        if offset is not None and (
            isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
        ):
            raise VaultError("recall response unavailable: malformed offset")
        effective_offset = 0 if offset is None else offset
        args: Dict[str, Any] = {"query": query, "limit": limit, "mode": mode}
        if category is not None:
            args["category"] = category
        if offset is not None:
            args["offset"] = offset
        args.update(extra)
        res = self.call_tool(self._tool("recall"), args)
        return self._normalize_recall_response(res, limit=limit, offset=effective_offset)

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
            page = self._normalize_items(res, offset=len(out))
            ids = {item["id"] for item in out}
            if any(item["id"] in ids for item in page):
                raise VaultError("scan response unavailable: duplicate item id")
            out.extend(page)
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
            ids = {item["id"] for item in out}
            if any(item["id"] in ids for item in page):
                raise VaultError("recall response unavailable: duplicate item id")
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
        """Soft-delete an entity. Returns True only if the vault found and archived it."""
        args: Dict[str, Any] = {"category": category, "key": key}
        if reason:
            args["reason"] = reason
        res = self.call_tool(self._tool("forget"), args)
        if not isinstance(res, dict):
            return False
        # #1024: current servers answer {"found": true|false}; pre-2.x servers
        # answered {"archived": N}. Accept either so the client stays compatible
        # across server versions.
        return bool(res.get("found") or res.get("archived", 0))

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
    def _validate_recall_projection_tree(value: Any, field: str, *, allowed: Optional[frozenset] = None, depth: int = 0) -> None:
        if depth > 8:
            raise VaultError("recall response unavailable: projection nesting exceeds bound")
        if allowed is not None and not isinstance(value, dict):
            raise VaultError("recall response unavailable: projection root must be an object")
        if isinstance(value, dict):
            if len(value) > 4096:
                raise VaultError("recall response unavailable: projection object is too large")
            accepted = _RECALL_PROJECTION_FIELDS if allowed is None else allowed
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise VaultError("recall response unavailable: malformed projection key")
                lowered = key.lower()
                if (
                    key not in accepted
                    or "raw" in lowered
                    or "private" in lowered
                    or "secret" in lowered
                    or "credential" in lowered
                    or "access_token" in lowered
                    or "api_key" in lowered
                ):
                    raise VaultError("recall response unavailable: unknown nested projection field")
                if key in _RECALL_PROJECTION_MAP_FIELDS:
                    if not isinstance(child, dict) or len(child) > 4096:
                        raise VaultError("recall response unavailable: malformed projection map")
                    for dynamic_key, dynamic_value in child.items():
                        if not isinstance(dynamic_key, str) or len(dynamic_key) > 256 or any(char.isspace() for char in dynamic_key):
                            raise VaultError("recall response unavailable: malformed projection map key")
                        VaultClient._validate_recall_projection_tree(
                            dynamic_value, f"{field}.{key}.{dynamic_key}", depth=depth + 1
                        )
                else:
                    VaultClient._validate_recall_projection_tree(child, f"{field}.{key}", depth=depth + 1)
        elif isinstance(value, list):
            if len(value) > 4096:
                raise VaultError("recall response unavailable: projection list is too large")
            for index, child in enumerate(value):
                VaultClient._validate_recall_projection_tree(child, f"{field}[{index}]", depth=depth + 1)
        elif isinstance(value, str):
            if len(value) > 1_048_576:
                raise VaultError("recall response unavailable: projection text is too large")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise VaultError("recall response unavailable: malformed projection value")

    @staticmethod
    def _validate_recall_projections(res: Dict[str, Any]) -> None:
        variants = res.get("variants")
        if "variants" in res and (isinstance(variants, bool) or not isinstance(variants, int) or variants < 0):
            raise VaultError("recall response unavailable: malformed variants")
        for field in _RECALL_OBJECT_FIELDS:
            if field in res:
                VaultClient._validate_recall_projection_tree(
                    res[field], field, allowed=_RECALL_PROJECTION_ROOT_FIELDS[field]
                )
        for field in _RECALL_STRING_FIELDS:
            if field in res and not isinstance(res[field], str):
                raise VaultError(f"recall response unavailable: malformed {field} projection")
        for field in _RECALL_BOOL_FIELDS:
            if field in res and not isinstance(res[field], bool):
                raise VaultError(f"recall response unavailable: malformed {field} projection")
        if "conflict_flags" in res:
            if not isinstance(res["conflict_flags"], list):
                raise VaultError("recall response unavailable: malformed conflict_flags projection")
            VaultClient._validate_recall_projection_tree(res["conflict_flags"], "conflict_flags")
        if "outcome" in res:
            outcome = res["outcome"]
            status = outcome.get("status")
            if not isinstance(status, str) or status.lower() not in _RECALL_OUTCOME_STATUS:
                raise VaultError("recall response unavailable: invalid outcome status")
        if "retrieval_profile" in res and (
            not isinstance(res["retrieval_profile"], str) or not res["retrieval_profile"]
        ):
            raise VaultError("recall response unavailable: malformed retrieval_profile")

    @staticmethod
    def _normalize_recall_response(res: Any, *, limit: int = 10, offset: int = 0) -> List[Dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise VaultError("recall response unavailable: malformed limit")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise VaultError("recall response unavailable: malformed offset")
        if not isinstance(res, dict) or "error" in res:
            raise VaultError("recall response unavailable: missing wire envelope")
        if set(res) - _RECALL_WIRE_FIELDS:
            raise VaultError("recall response unavailable: unknown wire field")
        VaultClient._validate_recall_projections(res)
        items = res.get("items")
        total = res.get("total")
        if not isinstance(items, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise VaultError("recall response unavailable: malformed total/items envelope")
        if total < len(items):
            raise VaultError("recall response unavailable: total below item count")
        expanded = "variants" in res
        if items and "retrieval_profile" not in res and not expanded:
            raise VaultError("recall response unavailable: missing retrieval_profile")
        outcome = res.get("outcome")
        if isinstance(outcome, dict):
            outcome_status = outcome.get("status")
            if not isinstance(outcome_status, str):
                raise VaultError("recall response unavailable: invalid outcome status")
            outcome_status = outcome_status.lower()
            if outcome_status in {"timeout", "stale", "unavailable", "partial", "degraded"}:
                raise VaultError(f"recall response unavailable: server outcome {outcome_status}")
            if outcome_status == "empty" and (items or total > offset):
                raise VaultError("recall response unavailable: inconsistent empty outcome")
            if outcome_status not in {"empty", "fresh", "complete"}:
                raise VaultError("recall response unavailable: unknown outcome status")
        remaining = max(0, total - offset)
        if offset > total:
            raise VaultError("recall response unavailable: offset exceeds total")
        if offset + len(items) > total:
            raise VaultError("recall response unavailable: page extends past total")
        if not items and remaining:
            raise VaultError("recall response unavailable: empty page has positive total")
        if items and len(items) < min(limit, remaining):
            raise VaultError("recall response unavailable: partial wire page")
        return VaultClient._normalize_items(res, offset=offset)

    @staticmethod
    def _recall_item_key(item: Dict[str, Any]) -> str:
        if "key" in item:
            item_id = item["key"]
        elif "id" in item:
            item_id = item["id"]
        else:
            raise VaultError("recall response unavailable: item lacks an id")
        if not isinstance(item_id, str) or not item_id:
            raise VaultError("recall response unavailable: item has an invalid id")
        return item_id

    @staticmethod
    def _recall_item_body(item: Dict[str, Any]) -> Dict[str, Any]:
        if "body_json" not in item:
            raise VaultError("recall response unavailable: item lacks body_json")
        body = item["body_json"]
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, TypeError) as exc:
                raise VaultError("recall response unavailable: malformed body_json") from exc
        if not isinstance(body, dict):
            raise VaultError("recall response unavailable: body_json must be an object")
        return body

    @staticmethod
    def _validate_recall_item(item: Dict[str, Any], index: int) -> None:
        unknown = set(item) - _RECALL_ITEM_FIELDS
        if unknown:
            raise VaultError(
                f"recall response unavailable: unknown item field {sorted(unknown)[0]}"
            )
        if "why_served" in item:
            projection = item["why_served"]
            if not isinstance(projection, dict) or set(projection) - _WHY_SERVED_FIELDS:
                raise VaultError("recall response unavailable: malformed why_served projection")
            for field, value in projection.items():
                if not isinstance(value, str) or not value or len(value) > 256:
                    raise VaultError("recall response unavailable: malformed why_served value")
        if "wire_rank" in item:
            rank = item["wire_rank"]
            if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
                raise VaultError("recall response unavailable: malformed wire rank")
        if "category" in item:
            category = item["category"]
            if not isinstance(category, str) or not category or len(category) > 256:
                raise VaultError("recall response unavailable: malformed category")
        for field in ("created_at_unix_ms", "updated_at_unix_ms", "last_accessed_unix_ms"):
            if field in item:
                timestamp = item[field]
                if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
                    raise VaultError(f"recall response unavailable: malformed {field}")

    @staticmethod
    def _normalize_items(res: Any, *, offset: int = 0) -> List[Dict[str, Any]]:
        if not isinstance(res, dict) or "items" not in res or not isinstance(res["items"], list):
            raise VaultError("recall response unavailable: malformed items envelope")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise VaultError("recall response unavailable: malformed offset")
        items = res["items"]
        out: List[Dict[str, Any]] = []
        seen_ids = set()
        for page_rank, it in enumerate(items, start=1):
            if not isinstance(it, dict):
                raise VaultError("recall response unavailable: malformed item")
            VaultClient._validate_recall_item(it, page_rank)
            item_id = VaultClient._recall_item_key(it)
            if item_id in seen_ids:
                raise VaultError("recall response unavailable: duplicate item id")
            seen_ids.add(item_id)
            body = VaultClient._recall_item_body(it)
            expected_wire_rank = offset + page_rank
            if "wire_rank" in it and it["wire_rank"] != expected_wire_rank:
                raise VaultError("recall response unavailable: inconsistent wire rank")
            score = it.get("score")
            if score is not None:
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                    raise VaultError("recall response unavailable: malformed semantic score")
                if "score_semantics" not in it:
                    raise VaultError("recall response unavailable: score requires explicit semantics")
                semantics = it["score_semantics"]
                if not isinstance(semantics, str) or not semantics:
                    raise VaultError("recall response unavailable: malformed score semantics")
                score = float(score)
            if "score_semantics" in it and score is None:
                raise VaultError("recall response unavailable: score_semantics without score")
            if "score_semantics" in it and not isinstance(it["score_semantics"], str):
                raise VaultError("recall response unavailable: malformed score semantics")
            decay_score = it.get("decay_score")
            if decay_score is not None:
                if isinstance(decay_score, bool) or not isinstance(decay_score, (int, float)) or not math.isfinite(float(decay_score)):
                    raise VaultError("recall response unavailable: malformed decay score")
            text = body.get("content", "")
            if not isinstance(text, str):
                raise VaultError("recall response unavailable: malformed body content")
            metadata = body.get("metadata", {})
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise VaultError("recall response unavailable: malformed body metadata")
            raw = {
                field: copy.deepcopy(it[field])
                for field in _RECALL_ITEM_FIELDS
                if field in it
            }
            raw["body_json"] = body
            raw["wire_rank"] = expected_wire_rank
            item = {
                "id": item_id,
                "text": text,
                "metadata": metadata,
                "score": score,
                **({"score_semantics": it["score_semantics"]} if score is not None else {}),
                "wire_rank": expected_wire_rank,
                "raw": raw,
            }
            if "decay_score" in it and it["decay_score"] is not None:
                item["decay_score"] = float(it["decay_score"])
            if "why_served" in it:
                item["why_served"] = copy.deepcopy(it["why_served"])
            out.append(item)
        return out


from .ephemeral import EphemeralAdmissionFixture
