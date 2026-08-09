#!/usr/bin/env python3
"""Perseus Vault Memory Provider Installer for Hermes Agent.

Installs the perseus-vault memory plugin on any machine with Hermes.
Run with: python3 install-perseus-vault.py

Non-interactive mode (token required; never a placeholder):
    PERSEUS_VAULT_MCP_TOKEN=<token> PERSEUS_VAULT_URL=http://host:port/message \
        python3 install-perseus-vault.py

Legacy aliases from the original installer are still accepted:
    MCP_PERSEUS_VAULT_API_KEY=<token> MCP_HOST_PORT=host:port

What the installer does:
  1. Writes the plugin to $HERMES_HOME/plugins/perseus-vault/
     (plugin.yaml, __init__.py, provider.py, cli.py)
  2. `hermes plugins enable perseus-vault`
  3. Persists the bearer token to $HERMES_HOME/.env as
     PERSEUS_VAULT_MCP_TOKEN (skipped if already present; .env backed up first)
  4. Writes non-secret config to $HERMES_HOME/perseus-vault.json (mode 600)
  5. `hermes config set memory.provider perseus-vault`
  6. Verifies with `hermes memory status`
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


class Colors:
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[1;33m"
    BLUE = "\033[0;34m"
    NC = "\033[0m"


def log_info(msg: str) -> None:
    print(f"{Colors.BLUE}[INFO]{Colors.NC} {msg}")


def log_ok(msg: str) -> None:
    print(f"{Colors.GREEN}[OK]{Colors.NC}  {msg}")


def log_warn(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.NC} {msg}")


def log_error(msg: str) -> None:
    print(f"{Colors.RED}[ERROR]{Colors.NC} {msg}")


def run_cmd(cmd: list, capture: bool = True) -> subprocess.CompletedProcess:
    """Run command and return result."""
    return subprocess.run(cmd, capture_output=capture, text=True)


def check_hermes() -> Optional[str]:
    """Check if Hermes is installed and return version."""
    if not shutil.which("hermes"):
        return None
    result = run_cmd(["hermes", "--version"])
    return result.stdout.strip().split("\n")[0] if result.stdout else "unknown"


def prompt_with_default(prompt: str, default: str) -> str:
    """Prompt user with default value. Falls back to default in non-interactive mode."""
    if not sys.stdin.isatty():
        return default
    if default:
        response = input(f"{prompt} [{default}]: ").strip()
        return response if response else default
    while True:
        response = input(f"{prompt}: ").strip()
        if response:
            return response
        print("  This field is required.")


def prompt_required(prompt: str) -> str:
    """Prompt until non-empty (interactive only)."""
    while True:
        response = input(f"{prompt}: ").strip()
        if response:
            return response
        print("  This field is required.")


def get_hermes_home() -> Path:
    """Get Hermes home directory."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


# ===========================================================================
# Plugin files content
# ===========================================================================

PLUGIN_YAML = r"""name: perseus-vault
version: "0.2.0"
description: "Perseus Vault memory provider via MCP HTTP — persistent, cross-session knowledge with semantic search and bi-temporal queries"
author: "Perseus Computing"
cli_commands:
  - perseus-vault
"""

INIT_PY = r'''"""Perseus Vault Memory Provider Plugin for Hermes Agent.

Registers the MemoryProvider implementation via the plugin system.
"""

from .provider import PerseusVaultProvider, create_provider


def register(ctx):
    """Plugin entry point - registers the MemoryProvider instance."""
    ctx.register_memory_provider(create_provider())


__all__ = ["PerseusVaultProvider", "create_provider", "register"]
'''

PROVIDER_PY = r'''"""Perseus Vault Memory Provider for Hermes Agent (v0.2.0).

Native MemoryProvider implementation backed by the Perseus Vault MCP HTTP
endpoint. Key properties (see integrations/hermes/README.md):

- prefetch() returns a real recall block; Hermes injects that string before
  turns (memory_manager.prefetch_all). This is the automatic per-turn
  memory surface. on_turn_start() and queue_prefetch() warm it in the
  background; on a cold start prefetch() falls back to a bounded synchronous
  recall (timeouts sized to the host's prefetch budget, issue #753 pattern).
- Curated tool allowlist: only safe read + scoped write tools are exposed to
  the agent. No purge/consolidate/authority/state/admin tools.
- Config resolution order: env vars (canonical PERSEUS_VAULT_*, legacy
  MCP_PERSEUS_VAULT_API_KEY / MCP_HOST_PORT) -> config.yaml
  memory.perseus-vault: -> $HERMES_HOME/perseus-vault.json -> defaults.
- sync_turn() is non-blocking (local buffer, flushed at session end).
- Session end runs a SCOPED capture (primary agent contexts only), not a
  global consolidate.
- workspace_hash is config-driven and only sent when set (blank = global).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://localhost:8767/message"
_PREFETCH_LIMIT = 6
_PREVIEW_CAP = 400
_SESSION_CAPTURE_MAX_CHARS = 8000
_TURN_BUFFER_CAP = 40
# The host (MemoryManager._prefetch_provider) runs prefetch() inside a thread
# it join()s with an ~8s budget; two serialized recall calls must fit.
_SYNC_CALL_TIMEOUT = 3.0
_WARM_CALL_TIMEOUT = 15.0

TOOL_ALLOWLIST = [
    "perseus_vault_context",
    "perseus_vault_recall",
    "perseus_vault_recall_when",
    "perseus_vault_semantic_search",
    "perseus_vault_stats",
    "perseus_vault_remember",
    "perseus_vault_forget",
    "perseus_vault_journal",
    "perseus_vault_capture",
]

# Static schemas (fetched from a live Perseus Vault MCP server >= 2.22.0).
# Static is REQUIRED: MemoryManager snapshots get_tool_schemas() BEFORE
# initialize() runs, so dynamically discovered schemas would be missing on
# turn 1. The agent never sees tools outside TOOL_ALLOWLIST.
SCHEMAS = json.loads(r"""[
 {
  "name": "perseus_vault_remember",
  "description": "Store or update an entity by (category, key). Idempotent \u2014 call as often as you want, same key returns an update. NEAR-DUPLICATE MERGING (#531): a NEW key whose body is >=70% trigram-similar to an existing entity in the same category+workspace does NOT create a new entity \u2014 the write is folded into the existing one (result: action='deduped', deduped=true, merged_into=<id>). Right for conversational memory; wrong for bulk ingest of templated records, which are similar by construction and will silently collapse to a handful of rows. For bulk ingest pass skip_dedup=true (or use perseus_vault_ingest_file), and check the returned action. Prefer recall_when triggers (retrieve when relevant) over always_on=true (inject unconditionally): the recall-first perseus_vault_context hard-caps the always-on set and warns when it overflows, so reserve always_on for genuinely identity-critical facts. Optional certainty (0.0-1.0) is used by perseus_vault_conflicts for typed-entity conflict detection. Pass derived_from (ids or {category,key} pairs of the memories you recalled) to auto-mark those sources useful \u2014 cited memories rank higher and decay slower. Use this for saving facts, decisions, architecture notes, and conventions. When encryption is enabled, body_json is encrypted at rest with AES-256-GCM.",
  "inputSchema": {
   "properties": {
    "agent_id": {
     "default": "",
     "description": "Agent identity (v1.2.0). Tracks which agent wrote this entity. Used for agent attribution and context filtering.",
     "type": "string"
    },
    "body_json": {
     "description": "JSON object with the entity body \u2014 store content, summary, and any custom fields here",
     "type": "string"
    },
    "category": {
     "description": "Entity category: 'decision', 'architecture', 'convention', 'insight', or custom",
     "type": "string"
    },
    "derived_from": {
     "description": "#487: the memories this write was built on (max 64). Each cited source is automatically marked useful \u2014 usefulness_count bumped, last_useful/last_accessed refreshed \u2014 so memories that actually inform later writes rank higher in recall and decay slower. Cite the entities you recalled before composing this write. Unknown citations are reported in the result, not fatal; self-citations are ignored.",
     "items": {
      "oneOf": [
       {
        "description": "Entity id of a cited source, e.g. 'mem-a1b2c3d4e5f6' (as returned by recall/remember)",
        "type": "string"
       },
       {
        "description": "A cited source addressed by (category, key)",
        "properties": {
         "category": {
          "type": "string"
         },
         "key": {
          "type": "string"
         }
        },
        "required": [
         "category",
         "key"
        ],
        "type": "object"
       }
      ]
     },
     "type": "array"
    },
    "external_refs": {
     "description": "#728: optional first-class pointers to external systems of record (max 32). Stored inside body_json under the reserved 'external_refs' key; filter recall with ref_type/ref_value.",
     "items": {
      "properties": {
       "ref_type": {
        "type": "string"
       },
       "ref_value": {
        "type": "string"
       },
       "relationship": {
        "enum": [
         "about",
         "derived_from",
         "mentions",
         "applies_to",
         "supersedes"
        ],
        "type": "string"
       },
       "source_system": {
        "type": "string"
       }
      },
      "required": [
       "ref_type",
       "ref_value"
      ],
      "type": "object"
     },
     "type": "array"
    },
    "importance": {
     "default": 0.5,
     "description": "Initial importance 0.0\u20131.0 \u2014 sets the starting decay score",
     "type": "number"
    },
    "key": {
     "description": "Unique key within the category, e.g. 'use-postgres-16' or 'deployment-strategy'",
     "type": "string"
    },
    "origin": {
     "description": "#729: optional memory-origin/provenance metadata (spec: docs/specs/memory-provenance-and-external-refs.md). Stored inside body_json under the reserved 'origin' key \u2014 surfaced by recall/get_entity via body expansion. All fields optional; unknown values are left absent, never guessed.",
     "properties": {
      "capture_method": {
       "type": "string"
      },
      "memory_kind": {
       "enum": [
        "asserted",
        "extracted",
        "inferred",
        "imported",
        "observed"
       ],
       "type": "string"
      },
      "observed_at_unix_ms": {
       "type": "integer"
      },
      "source_system": {
       "type": "string"
      }
     },
     "type": "object"
    },
    "skip_dedup": {
     "default": false,
     "description": "Opt out of near-duplicate merging for this write (#531). Set true for bulk/API ingest of templated records so every acknowledged write actually creates its key; leave false for conversational memory.",
     "type": "boolean"
    },
    "status": {
     "default": "active",
     "description": "Entity status: 'active', 'draft', 'deprecated'",
     "type": "string"
    },
    "tags": {
     "description": "Tags for categorization and cross-referencing",
     "items": {
      "type": "string"
     },
     "type": "array"
    },
    "topic_path": {
     "default": "",
     "description": "Hierarchical topic path, e.g. 'architecture/database/postgres'",
     "type": "string"
    },
    "type": {
     "default": "insight",
     "description": "Entity type: 'insight', 'architecture', 'decision', 'reference', 'convention'",
     "type": "string"
    },
    "valid_from_unix_ms": {
     "description": "Application-time period start (#363): when the fact became TRUE IN THE WORLD, independent of when it was recorded. Set in the past for retroactive facts ('this was true last week, we just learned it') without rewriting transaction history. Default: transaction time (now). Query with mimir_valid_at / mimir_bitemporal / recall's valid_at filter.",
     "type": "integer"
    },
    "valid_to_unix_ms": {
     "description": "Application-time period end (#363, exclusive): when the fact STOPPED being true in the world. Omit for 'still true' (unbounded). Must be greater than valid_from_unix_ms.",
     "type": "integer"
    },
    "workspace_hash": {
     "default": "",
     "description": "Workspace scope identifier (v1.2.0). Empty = global. Entities with a workspace_hash are invisible to recall queries scoped to a different workspace.",
     "type": "string"
    }
   },
   "required": [
    "category",
    "key",
    "body_json"
   ],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_recall",
  "description": "Search entities with FTS5 keyword search. Words are OR'd together. Returns entities sorted by relevance with expanded content/summary fields at top level. Use this to find previously stored facts, decisions, or architecture notes. When encryption is enabled, body_json is decrypted transparently.",
  "inputSchema": {
   "properties": {
    "agent_id": {
     "description": "Agent identity filter (v1.2.0). When set, only entities with a matching agent_id are returned. Omit for no agent filtering.",
     "type": "string"
    },
    "as_of_unix_ms": {
     "description": "#472 Temporal RAG: transaction-time instant (unix ms). Reconstruct semantic recall AS BELIEVED at this past instant \u2014 each hit's body is the version that was live at as_of_unix_ms; corrections recorded later do not leak in. Combine with valid_at for the full bi-temporal cell. Hits are stamped with is_live_version / recorded_at_unix_ms / valid_from_unix_ms / valid_to_unix_ms. Omit for today's live view. (v1: candidate generation is over the live index, so a fact fully deleted since that instant will not surface.)",
     "type": "integer"
    },
    "category": {
     "description": "Filter by category, e.g. 'decision' or 'architecture'",
     "type": "string"
    },
    "content_weight": {
     "default": 0,
     "description": "Additive boost for content witness \u2014 rewards entities whose body text literally contains query terms. Damped by body length. Never penalizes.",
     "maximum": 1,
     "minimum": 0,
     "type": "number"
    },
    "diversity_halving": {
     "default": 1,
     "description": "Per-keyword diversity quota factor (1.0=disabled). Each distinct matched keyword gets ceil(N x halving^n) slots \u2014 first keyword N, second N/2, etc.",
     "maximum": 1,
     "minimum": 0,
     "type": "number"
    },
    "expansion": {
     "description": "Configuration for FTS5 query expansion using Porter stemming",
     "properties": {
      "enabled": {
       "default": false,
       "description": "Enable stemming-based query expansion",
       "type": "boolean"
      },
      "n_variants": {
       "default": 1,
       "description": "Number of stemmed token variants to generate",
       "type": "integer"
      }
     },
     "type": "object"
    },
    "include_archived": {
     "default": false,
     "description": "Include archived (soft-deleted) entities in results",
     "type": "boolean"
    },
    "include_confidence": {
     "default": false,
     "description": "Add a normalized confidence score (0.0-1.0) to each result, rolled up from rank, trust (verified/certainty), and decay. Presentation-only; does not change ranking.",
     "type": "boolean"
    },
    "layer": {
     "description": "Filter by memory layer (world, episodic, semantic).",
     "type": "string"
    },
    "limit": {
     "default": 10,
     "description": "Maximum number of results to return (max 1000)",
     "type": "integer"
    },
    "min_decay": {
     "default": 0.0,
     "description": "Minimum decay score threshold 0.0\u20131.0 \u2014 higher values return fresher results",
     "type": "number"
    },
    "mode": {
     "default": "fts5",
     "description": "Search mode: 'fts5' (keyword), 'dense' (vector), or 'hybrid' (fused via RRF)",
     "enum": [
      "fts5",
      "dense",
      "hybrid"
     ],
     "type": "string"
    },
    "offset": {
     "default": 0,
     "description": "Number of results to skip for pagination",
     "type": "integer"
    },
    "preview_cap": {
     "description": "If set, truncate body_json at N chars and append drill-down footer. Use mimir_get_entity to read full body.",
     "type": "integer"
    },
    "query": {
     "description": "Search query \u2014 words are OR'd together for broad recall. An EMPTY string (\"\") is the match-all / enumeration path: it drops the keyword predicate and returns every entity in scope (respecting category/type/limit/offset), so it is the way to 'list all' a category. Wildcards are NOT globs: \"*\" is a literal FTS5 term and matches nothing \u2014 pass \"\" to enumerate, not \"*\".",
     "type": "string"
    },
    "recency_half_life_secs": {
     "description": "Time-aware ranking for mode='hybrid' (default off). When set, each fused result's score is multiplied by 0.5^(age / this), where age is seconds since the memory was created \u2014 so a memory this many seconds old keeps half its weight and recent context outranks older but similar hits. Omit for relevance-only ranking.",
     "minimum": 0,
     "type": "number"
    },
    "ref_type": {
     "description": "#728: post-filter hits to entities whose body external_refs carry this ref_type (exact match, e.g. 'repo', 'pull_request', 'jira_key').",
     "type": "string"
    },
    "ref_value": {
     "description": "#728: post-filter hits to entities whose body external_refs carry this ref_value. Matches exactly or as a hierarchical '/' prefix ('github:Org' matches 'github:Org/repo').",
     "type": "string"
    },
    "reinforce": {
     "default": false,
     "description": "Opt-in reinforcement for mode='dense'/'hybrid': bump retrieval_count/last_accessed/decay on the returned hits so semantically-used memories resist decay and promote through layers. Default false keeps semantic recall side-effect-free and byte-deterministic over a frozen DB. No effect on mode='fts5', which already reinforces.",
     "type": "boolean"
    },
    "retrieval_profile": {
     "description": "#784 serving posture. personal returns preference/personal classes; agent returns convention/correction/keystone classes; shared (default) returns non-personal memory in the requested workspace. Applied after visibility filtering.",
     "enum": [
      "personal",
      "agent",
      "shared"
     ],
     "type": "string"
    },
    "scope_weight": {
     "description": "#485: scope as a ranking multiplier instead of a hard filter. Requires workspace_hash. Widens the workspace filter to also include GLOBAL (workspace_hash='') memories, weighted by this factor in the ranking (hybrid/dense scores multiplied; keyword mode returns current-scope hits first) \u2014 current-workspace memories outrank equally-relevant global ones, but a strong global memory still surfaces. Never exposes other workspaces' memories. Omit for the strict filter (unchanged default).",
     "maximum": 1,
     "minimum": 0,
     "type": "number"
    },
    "topic_path": {
     "description": "Filter by topic path prefix, e.g. 'architecture/'",
     "type": "string"
    },
    "trust_weight": {
     "default": 0.15,
     "description": "Additive boost for provenance/trust (default 0.15, on by default) \u2014 verified sources rank above unverified AI drafts on the same topic. Verified entities get the full boost; unverified ones are scaled by certainty. Set 0 to disable. Never penalizes.",
     "maximum": 1,
     "minimum": 0,
     "type": "number"
    },
    "type": {
     "description": "Filter by entity type, e.g. 'insight' or 'reference'",
     "type": "string"
    },
    "valid_at": {
     "description": "Valid-time instant (#363/#472, unix ms): reconstruct recall to the world-version whose application-time period [valid_from, valid_to) contains this instant \u2014 'what was true at time T', per current (or as_of) knowledge. Rebuilds the point-in-time body from history (not just a live-row narrow) and returns hits stamped with is_live_version / recorded_at_unix_ms / valid_from/to. Combine with as_of_unix_ms for the full bi-temporal cell.",
     "type": "integer"
    },
    "valid_from_unix_ms": {
     "description": "Valid-time period filter start (#363, unix ms). Pair with valid_to_unix_ms and valid_op; ignored when valid_at is set. Omit for unbounded start.",
     "type": "integer"
    },
    "valid_op": {
     "default": "overlaps",
     "description": "SQL:2011 period predicate for the valid-time period filter (#363): 'overlaps' (fact's valid period shares at least one instant with the queried period) or 'contains' (fact's valid period contains the whole queried period).",
     "enum": [
      "overlaps",
      "contains"
     ],
     "type": "string"
    },
    "valid_to_unix_ms": {
     "description": "Valid-time period filter end (#363, unix ms, exclusive). Omit for unbounded end.",
     "type": "integer"
    },
    "workspace_hash": {
     "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash are returned. Omit for no workspace filtering.",
     "type": "string"
    }
   },
   "required": [
    "query"
   ],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_semantic_search",
  "description": "Dense-only semantic search: find entities by meaning, ranked purely by embedding similarity (no keyword fallback). On by default via the bundled in-process ONNX model \u2014 zero config, zero network. A one-tool shortcut for 'find things like this'. For fused keyword+vector results use perseus_vault_recall.",
  "inputSchema": {
   "properties": {
    "agent_id": {
     "description": "Agent identity filter. When set, only entities with a matching agent_id are returned.",
     "type": "string"
    },
    "category": {
     "description": "Filter by category, e.g. 'decision' or 'architecture'",
     "type": "string"
    },
    "limit": {
     "default": 10,
     "description": "Maximum number of results to return",
     "type": "integer"
    },
    "query": {
     "description": "Natural-language text to semantically match against stored memories",
     "type": "string"
    },
    "workspace_hash": {
     "description": "Workspace scope filter. When set, only entities with a matching workspace_hash are returned.",
     "type": "string"
    }
   },
   "required": [
    "query"
   ],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_forget",
  "description": "Soft-delete an entity by setting archived=1. The entity is hidden from queries but recoverable. Use this to clean up stale or incorrect facts without permanent data loss.",
  "inputSchema": {
   "properties": {
    "category": {
     "description": "Entity category to archive",
     "type": "string"
    },
    "key": {
     "description": "Entity key to archive",
     "type": "string"
    },
    "reason": {
     "default": "",
     "description": "Reason for archiving, logged for audit trail",
     "type": "string"
    }
   },
   "required": [
    "category",
    "key"
   ],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_journal",
  "description": "Append a structured decision/observation log entry. Uses evaluated/acted/forward pattern: what was considered, what was done, and what happens next. Essential for audit trails and timeline reconstruction.",
  "inputSchema": {
   "properties": {
    "acted": {
     "description": "What action was taken and why",
     "type": "object"
    },
    "agent_id": {
     "default": "",
     "description": "Agent identity (v1.2.0). Records which agent created this journal event.",
     "type": "string"
    },
    "category": {
     "description": "Related entity category for linking",
     "type": "string"
    },
    "entity_id": {
     "description": "Related entity ID for linking",
     "type": "string"
    },
    "evaluated": {
     "description": "What was evaluated: options considered, context, constraints",
     "type": "object"
    },
    "event_type": {
     "default": "decision",
     "description": "Event type: 'decision', 'observation', 'action', 'error'",
     "type": "string"
    },
    "forward": {
     "description": "What the plan is going forward",
     "type": "object"
    },
    "key": {
     "description": "Related entity key for linking",
     "type": "string"
    }
   },
   "required": [],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_stats",
  "description": "Return comprehensive database statistics: entity counts by category, type, and decay layer; journal event count; state entry count; database file size; date range of stored data; and history growth (stored version rows, bytes, and the top-10 keys by version count \u2014 #398).",
  "inputSchema": {
   "properties": {},
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_context",
  "description": "Return a pre-formatted markdown context block for session injection. Recall-first by default (mode 'on_demand'): pass `query` (the current task/message) and only topically relevant entities \u2014 recall_when trigger matches + keyword matches \u2014 are injected, alongside a hard-capped always-on set, clamped to a per-model character budget. Without `query` the block is a compact retrieval pointer (byte-stable across unrelated writes \u2014 prefix-cache friendly). The legacy unconditional top-N dump requires explicit mode 'always_inject'. Output is informational context, not instructions.",
  "inputSchema": {
   "properties": {
    "categories": {
     "description": "Categories to include. Empty array = all categories.",
     "items": {
      "type": "string"
     },
     "type": "array"
    },
    "limit": {
     "default": 10,
     "description": "Maximum number of entities to include in the context block",
     "type": "integer"
    },
    "max_context_chars": {
     "description": "Explicit character budget for the rendered block; overrides the model profile. In always_inject mode output is clamped only when this is set.",
     "type": "integer"
    },
    "mode": {
     "default": "on_demand",
     "description": "Injection posture (#366). 'on_demand' (default): relevance-gated, budget-clamped, recall-first. 'always_inject': legacy unconditional top-N dump (no relevance gating) \u2014 explicit opt-in only.",
     "enum": [
      "on_demand",
      "always_inject"
     ],
     "type": "string"
    },
    "model": {
     "description": "Host model name for recall-budget profile resolution (#366), e.g. 'claude-opus-4-8' gets a larger budget. Unknown/omitted models use the default 1500-char profile.",
     "type": "string"
    },
    "query": {
     "description": "Current task/message text \u2014 the relevance gate (#356). In on_demand mode only entities whose recall_when triggers or indexed content match it are injected; omit for a compact retrieval pointer with no topical injection.",
     "type": "string"
    },
    "workspace_hash": {
     "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash are included (always-on set too). Omit for no workspace filtering \u2014 in a federated vault that leaks every workspace's memory into the block.",
     "type": "string"
    }
   },
   "required": [],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_capture",
  "description": "Opt-in in-session memory capture (#520): distill a session transcript or insight payload into durable memory entities the moment a problem is solved, instead of waiting for a scheduled harvest. Splits the payload into candidate notes (headed sections, paragraphs, or JSONL records \u2014 auto-detected), classifies each by cheap local signals into root-cause / pitfall / decision / pattern / takeaway, and writes each through the normal remember path with source='capture' (layer buffer, moderate importance). Fully local and deterministic by default \u2014 no LLM, no network; pass llm=true to distill via the configured --llm-endpoint instead (falls back to the rule-based path on any LLM failure or timeout). Anti-flood by design: near-duplicate merging stays ON (a re-captured solved problem merges into the existing memory), same-headline notes update in place, and writes are capped per invocation with dropped notes reported. Nothing runs automatically \u2014 capture happens only when this tool (or the `perseus-vault capture` CLI verb) is explicitly invoked, e.g. from an on_insight or SessionEnd lifecycle hook (run `maintain` after end-of-session capture).",
  "inputSchema": {
   "properties": {
    "agent_id": {
     "description": "Agent ID recorded on the captured entities.",
     "type": "string"
    },
    "consume": {
     "default": false,
     "description": "#563: after a SUCCESSFUL non-dry-run capture, atomically remove exactly the captured regions from source_file (temp file + rename, leaving a <source_file>.bak). Scoped to captured records only \u2014 surrounding headers/rules/pointers are left untouched. No-op under dry_run, when nothing was captured, or when source_file is unset, so it can never delete content that was not durably stored. Use it to keep a host-inlined write-buffer (e.g. an AGENTS.local.md the agent loads every turn) from accumulating already-stored blocks forever. The result reports 'consumed' (regions removed) and 'source_backup'.",
     "type": "boolean"
    },
    "dry_run": {
     "default": false,
     "description": "Distill and return the would-be notes without writing anything.",
     "type": "boolean"
    },
    "llm": {
     "default": false,
     "description": "Distill via the configured LLM endpoint instead of the local rule-based distiller. Requires --llm-endpoint; falls back to the rule-based path on any LLM failure (the result's llm_fallback field says why).",
     "type": "boolean"
    },
    "max_entities": {
     "default": 20,
     "description": "Anti-flood cap: max entities written by this invocation (1-20; callers can lower the cap, not raise it). Notes beyond the cap are dropped and counted in the result.",
     "type": "integer"
    },
    "source_file": {
     "description": "#563: path to the file the payload came from. Required for consume to have anything to prune; ignored when consume is false.",
     "type": "string"
    },
    "text": {
     "description": "The transcript / insight payload to distill. Plain text, markdown (headed sections become separate notes), or JSONL (one note per record, using its content/text/insight/lesson/summary/message field).",
     "type": "string"
    },
    "workspace_hash": {
     "description": "Workspace hash to scope the captured entities to. Omit for unscoped (global) capture.",
     "type": "string"
    }
   },
   "required": [
    "text"
   ],
   "type": "object"
  }
 },
 {
  "name": "perseus_vault_recall_when",
  "description": "Search entities whose recall_when triggers match a given context. Use this for proactive just-in-time memory injection \u2014 before writing code, before plans, at session start. Pass the current task description as context and get back memories that declared they should be recalled in similar situations.",
  "inputSchema": {
   "properties": {
    "context": {
     "description": "The current task or context description to match against recall_when triggers",
     "type": "string"
    },
    "limit": {
     "default": 10,
     "description": "Maximum entities to return (default 10, max 100)",
     "type": "integer"
    },
    "workspace_hash": {
     "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash can fire. Omit for no workspace filtering \u2014 in a federated vault that lets one workspace's triggers inject into another's turns.",
     "type": "string"
    }
   },
   "required": [
    "context"
   ],
   "type": "object"
  }
 }
]""")


class MCPClient:
    """Minimal MCP HTTP client for Perseus Vault; returns {ok, text, data}."""

    PROTOCOL = "2025-06-18"
    CLIENT_INFO = {"name": "hermes-perseus-vault", "version": "0.2.0"}

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.next_id = 1

    def _request(self, method: str, params: dict | None = None,
                 timeout: float = 30.0) -> dict:
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params

        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e.reason}")
        if not body.strip():
            return {}
        # Defensive: streamable HTTP may frame with SSE even when JSON was
        # requested. Prefer JSON; fall back to the last data: payload.
        if body.lstrip().startswith(("event:", "data:")):
            lines = [l[5:].strip() for l in body.splitlines()
                     if l.startswith("data:")]
            body = lines[-1] if lines else body
        return json.loads(body)

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )

    def list_tools(self) -> list:
        result = self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float = 30.0) -> dict:
        """Call a Vault MCP tool; returns {"ok": bool, "text": str, "data": Any}."""
        raw = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}},
                            timeout=timeout)
        if "error" in raw:
            return {"ok": False, "text": json.dumps(raw["error"]), "data": None}
        result = raw.get("result", {}) or {}
        is_error = bool(result.get("isError", False))
        parts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
        data = result.get("structuredContent")
        if not text and data is not None:
            text = json.dumps(data)
        return {"ok": not is_error, "text": text, "data": data}


class PerseusVaultProvider(MemoryProvider):
    """Perseus Vault memory provider via MCP HTTP with lifecycle hooks."""

    name = "perseus-vault"

    def __init__(self):
        self._client: Optional[MCPClient] = None
        self._session_id: str = ""
        self._agent_context: str = "primary"
        self._workspace_hash: str = ""
        self._enabled: bool = False
        self._prefetched: str = ""
        self._prefetch_lock = threading.Lock()
        self._turn_buffer: List[Dict[str, str]] = []
        self._config: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Config resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _config_section() -> Dict[str, Any]:
        try:
            from hermes_cli.config import cfg_get, load_config
            config = load_config()
            section = cfg_get(config, "memory", "perseus-vault")
            return section if isinstance(section, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _json_config() -> Dict[str, str]:
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        except Exception:
            home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        path = Path(home) / "perseus-vault.json"
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_config(self) -> None:
        section = self._config_section()
        jcfg = self._json_config()
        url = (os.getenv("PERSEUS_VAULT_URL", "").strip()
               or os.getenv("PERSEUS_MCP_URL", "").strip()
               or str(section.get("url", "") or "").strip()
               or str(jcfg.get("url", "") or "").strip()
               or _DEFAULT_URL)
        token = (os.getenv("PERSEUS_VAULT_MCP_TOKEN", "").strip()
                 or os.getenv("MCP_PERSEUS_VAULT_API_KEY", "").strip()
                 or str(section.get("token", "") or "").strip()
                 or str(jcfg.get("api_key", "") or "").strip())
        ws = (os.getenv("PERSEUS_VAULT_WORKSPACE", "").strip()
              or str(section.get("workspace_hash", "") or "").strip()
              or str(jcfg.get("workspace_hash", "") or "").strip())
        self._config = {"url": url, "api_key": token, "workspace_hash": ws}
        self._workspace_hash = ws

    def _ws_args(self) -> Dict[str, str]:
        return ({"workspace_hash": self._workspace_hash}
                if self._workspace_hash else {})

    # ------------------------------------------------------------------
    # Core lifecycle
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Quick health check — no network calls per contract."""
        self._load_config()
        return bool(self._config.get("url") and self._config.get("api_key"))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")
        self._load_config()
        if not self._config.get("api_key"):
            logger.warning(
                "perseus-vault: no token configured (env PERSEUS_VAULT_MCP_TOKEN, "
                "config memory.perseus-vault.token, or perseus-vault.json); "
                "provider disabled")
            self._enabled = False
            return
        try:
            self._client = MCPClient(self._config["url"], self._config["api_key"])
            init_result = self._client.initialize()
            if "error" in init_result:
                raise RuntimeError(f"MCP initialize failed: {init_result['error']}")
            tools = self._client.list_tools()
            logger.info(
                "Perseus Vault connected: %s MCP tools available "
                "(%d exposed to agent)",
                len(tools), len(TOOL_ALLOWLIST))
            self._enabled = True
        except Exception as e:
            logger.warning("perseus-vault: connect failed, provider disabled: %s", e)
            self._client = None
            self._enabled = False

    def shutdown(self) -> None:
        self._client = None
        self._enabled = False

    # ------------------------------------------------------------------
    # Prompt + prefetch (the actual injection surface)
    # ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if not self._enabled:
            return ""
        return (
            "## Long-term memory: Perseus Vault\n"
            "A shared Perseus Vault is connected as the external memory provider. "
            "Relevant Vault memories are prefetched and injected before turns when "
            "available. Use `perseus_vault_recall` to search it before asking the "
            "user to repeat context, and `perseus_vault_remember` to persist "
            "durable facts, decisions, and corrections. Never store secret values."
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._enabled or not self._client or not query.strip():
            return

        def _work() -> None:
            block = self._build_recall_block(query, call_timeout=_WARM_CALL_TIMEOUT)
            with self._prefetch_lock:
                self._prefetched = block

        threading.Thread(target=_work, daemon=True,
                         name="perseus-vault-prefetch").start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return recalled context; Hermes injects non-empty results."""
        with self._prefetch_lock:
            block = self._prefetched
            self._prefetched = ""
        if block:
            return block
        if not self._enabled or not self._client or not query.strip():
            return ""
        try:
            # Cold-start path (issue #753): bounded synchronous recall inside
            # the host's prefetch thread budget.
            return self._build_recall_block(query, call_timeout=_SYNC_CALL_TIMEOUT)
        except Exception as e:
            logger.debug("perseus-vault: synchronous prefetch failed: %s", e)
            return ""

    def _build_recall_block(self, query: str,
                            call_timeout: float = _WARM_CALL_TIMEOUT) -> str:
        items: List[Dict[str, Any]] = []
        seen: set = set()

        res = self._client.call_tool(
            "perseus_vault_recall_when",
            {"context": query, "limit": 4, **self._ws_args()},
            timeout=call_timeout)
        for it in self._extract_items(res):
            if it.get("id") not in seen:
                seen.add(it.get("id"))
                items.append(it)

        res = self._client.call_tool(
            "perseus_vault_recall",
            {"query": query, "limit": _PREFETCH_LIMIT,
             "preview_cap": _PREVIEW_CAP, **self._ws_args()},
            timeout=call_timeout)
        for it in self._extract_items(res):
            if it.get("id") not in seen:
                seen.add(it.get("id"))
                items.append(it)

        if not items:
            return ""
        lines = ["## Recalled from Perseus Vault (shared memory)"]
        for it in items[:_PREFETCH_LIMIT + 2]:
            text = self._item_text(it)
            if text:
                lines.append(f"- [{it.get('category', '')}/{it.get('key', '')}] {text}")
        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _extract_items(res: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not res.get("ok"):
            return []
        payload = res.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(res.get("text", "") or "{}")
            except Exception:
                return []
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                payload = json.loads(inner)
            except Exception:
                return []
        items = payload.get("entities") or payload.get("items") or []
        return items if isinstance(items, list) else []

    @staticmethod
    def _item_text(it: Dict[str, Any]) -> str:
        text = it.get("summary") or it.get("content") or it.get("body") or ""
        if isinstance(text, (dict, list)):
            text = json.dumps(text)
        text = str(text).strip().replace("\n", " ")
        return text[:_PREVIEW_CAP]

    # ------------------------------------------------------------------
    # Turn hooks
    # ------------------------------------------------------------------

    def on_turn_start(self, turn: int, message: str, **kwargs) -> None:
        """Warm the prefetch cache for the next turn (results are NOT
        injected by Hermes; only prefetch()'s return value is)."""
        if turn == 1 and message:
            self.queue_prefetch(message)

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages=None) -> None:
        """Non-blocking: buffer locally, flush at session end."""
        if not self._enabled:
            return
        self._turn_buffer.append({
            "user": (user_content or "")[:2000],
            "assistant": (assistant_content or "")[:2000],
        })
        if len(self._turn_buffer) > _TURN_BUFFER_CAP:
            self._turn_buffer = self._turn_buffer[-_TURN_BUFFER_CAP:]

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Scoped session capture (primary agent contexts only). No global
        consolidate — that is the server cron's nightly job."""
        if not self._enabled or not self._client:
            return
        if self._agent_context != "primary":
            return  # cron/subagent sessions would pollute shared memory
        source = messages if messages else [
            {"role": "user", "content": t["user"]} for t in self._turn_buffer
        ]
        text_parts: List[str] = []
        total = 0
        for msg in source:
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content") or ""
            if not isinstance(content, str) or not content.strip():
                continue
            chunk = f"{role.upper()}: {content.strip()}"
            if total + len(chunk) > _SESSION_CAPTURE_MAX_CHARS:
                break
            text_parts.append(chunk)
            total += len(chunk)
        if not text_parts:
            return
        transcript = (
            "Session transcript to distill into durable memories "
            "(facts, decisions, corrections, lessons only; skip chit-chat; "
            "never include secret values):\n\n" + "\n\n".join(text_parts)
        )
        try:
            self._client.call_tool(
                "perseus_vault_capture",
                {"text": transcript, "max_entities": 5},
                timeout=60)
            self._turn_buffer.clear()
        except Exception as e:
            logger.warning("perseus-vault session capture failed: %s", e)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Best-effort capture before context compression; never blocks."""
        if not self._enabled or not self._client or not messages:
            return ""
        try:
            session_text = "\n".join(
                f"{m.get('role', 'unknown')}: {str(m.get('content', ''))[:400]}"
                for m in messages[-30:])
            self._client.call_tool(
                "perseus_vault_capture",
                {"text": session_text[:_SESSION_CAPTURE_MAX_CHARS],
                 "max_entities": 5},
                timeout=30)
            return "Session insights captured to Vault"
        except Exception:
            return ""

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict] = None) -> None:
        """Mirror built-in memory writes to the Vault."""
        if not self._enabled or not self._client or not content:
            return
        digest = hashlib.sha1(content.encode()).hexdigest()[:8]
        category = "hermes-memory"
        key = f"builtin-{target}-{digest}"
        try:
            if action in ("add", "replace"):
                self._client.call_tool("perseus_vault_remember", {
                    "category": category,
                    "key": key,
                    "body_json": json.dumps({
                        "summary": content[:600],
                        "source": "hermes-builtin-memory",
                        "target": target,
                    }),
                    "type": "insight",
                    "tags": ["hermes", "builtin-memory", target],
                    **self._ws_args(),
                }, timeout=15)
            elif action == "remove":
                self._client.call_tool("perseus_vault_forget", {
                    "category": category, "key": key,
                    "reason": "removed from built-in memory",
                    **self._ws_args(),
                }, timeout=15)
        except Exception:
            pass  # Non-blocking

    # ------------------------------------------------------------------
    # Tools (curated allowlist)
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> list:
        # Static: MemoryManager snapshots schemas BEFORE initialize() runs.
        return SCHEMAS

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                         **kwargs) -> str:
        if not self._enabled or not self._client:
            return json.dumps({"success": False,
                               "error": "perseus-vault not connected"})
        if tool_name not in TOOL_ALLOWLIST:
            return json.dumps({"success": False,
                               "error": f"tool {tool_name} is not exposed by "
                                        f"the perseus-vault provider"})
        try:
            call_args = dict(args or {})
            if tool_name in ("perseus_vault_recall", "perseus_vault_recall_when",
                             "perseus_vault_semantic_search", "perseus_vault_context",
                             "perseus_vault_remember", "perseus_vault_forget",
                             "perseus_vault_capture"):
                call_args.update(self._ws_args())
            if tool_name == "perseus_vault_recall" and "preview_cap" not in call_args:
                call_args["preview_cap"] = _PREVIEW_CAP
            res = self._client.call_tool(tool_name, call_args, timeout=20)
            if tool_name in ("perseus_vault_recall", "perseus_vault_recall_when",
                             "perseus_vault_semantic_search", "perseus_vault_context"):
                items = self._extract_items(res)
                return json.dumps({
                    "success": res.get("ok", False),
                    "count": len(items),
                    "items": [{
                        "category": it.get("category", ""),
                        "key": it.get("key", ""),
                        "text": self._item_text(it),
                    } for it in items],
                })
            return json.dumps({
                "success": res.get("ok", False),
                "result": res.get("text", "")[:800],
            })
        except Exception as e:
            logger.error("Tool call %s failed: %s", tool_name, e)
            return json.dumps({"success": False, "error": str(e)})

    # ------------------------------------------------------------------
    # Setup wizard integration
    # ------------------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "token",
                "description": "Perseus Vault MCP bearer token",
                "secret": True,
                "required": True,
                "env_var": "PERSEUS_VAULT_MCP_TOKEN",
                "url": "https://github.com/Perseus-Computing-LLC/perseus-vault",
            },
            {
                "key": "url",
                "description": "Vault MCP endpoint URL",
                "required": False,
                "default": _DEFAULT_URL,
                "env_var": "PERSEUS_VAULT_URL",
            },
            {
                "key": "workspace_hash",
                "description": "Workspace scope hash (blank = global/unscoped)",
                "required": False,
                "default": "",
                "env_var": "PERSEUS_VAULT_WORKSPACE",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to $HERMES_HOME/perseus-vault.json."""
        try:
            path = Path(hermes_home) / "perseus-vault.json"
            data = {"url": values.get("url") or _DEFAULT_URL,
                    "workspace_hash": values.get("workspace_hash", "")}
            path.write_text(json.dumps(data, indent=2))
            path.chmod(0o600)
        except Exception as e:
            logger.warning("perseus-vault: save_config failed: %s", e)

    def backup_paths(self) -> List[str]:
        return [
            "~/.hermes/perseus-vault.json",
            "~/.hermes/plugins/perseus-vault/",
        ]

    def get_status_config(self, provider_config: Dict[str, Any]) -> Dict[str, Any]:
        """Redacted view for `hermes memory status`."""
        self._load_config()
        return {
            "url": self._config.get("url", _DEFAULT_URL),
            "workspace_hash": self._workspace_hash or "(global)",
            "token": "set" if self._config.get("api_key") else "MISSING",
        }


def create_provider() -> PerseusVaultProvider:
    """Factory function for plugin registration."""
    return PerseusVaultProvider()
'''

CLI_PY = r'''"""CLI commands for Perseus Vault memory provider.

Registers subcommands under `hermes perseus-vault <cmd>`.
Only available when perseus-vault is the active memory provider.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _get_config() -> dict:
    """Load provider config: env (canonical + legacy) -> config.yaml
    memory.perseus-vault: -> perseus-vault.json."""
    config: dict = {}
    try:
        from hermes_cli.config import cfg_get, load_config
        section = cfg_get(load_config(), "memory", "perseus-vault")
        if isinstance(section, dict):
            config.update({k: v for k, v in section.items() if k != "token"})
    except Exception:
        pass
    try:
        jpath = get_hermes_home() / "perseus-vault.json"
        if jpath.exists():
            config.update(json.loads(jpath.read_text()))
    except Exception:
        pass
    if url := os.getenv("PERSEUS_VAULT_URL") or os.getenv("PERSEUS_MCP_URL"):
        config["url"] = url
    if token := (os.getenv("PERSEUS_VAULT_MCP_TOKEN")
                 or os.getenv("MCP_PERSEUS_VAULT_API_KEY")):
        config["api_key"] = token
    if ws := os.getenv("PERSEUS_VAULT_WORKSPACE"):
        config["workspace_hash"] = ws
    return config


class MCPClient:
    """Minimal MCP HTTP client (mirrors provider.py)."""

    PROTOCOL = "2025-06-18"
    CLIENT_INFO = {"name": "hermes-perseus-vault-cli", "version": "0.2.0"}

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self.next_id = 1

    def _request(self, method: str, params: dict | None = None) -> dict:
        import urllib.error
        import urllib.request

        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        self.next_id += 1
        if params is not None:
            message["params"] = params
        data = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:400]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Connection error: {e.reason}")
        if not body.strip():
            return {}
        if body.lstrip().startswith(("event:", "data:")):
            lines = [l[5:].strip() for l in body.splitlines()
                     if l.startswith("data:")]
            body = lines[-1] if lines else body
        return json.loads(body)

    def initialize(self) -> dict:
        return self._request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL,
                "capabilities": {},
                "clientInfo": self.CLIENT_INFO,
            },
        )

    def list_tools(self) -> list:
        result = self._request("tools/list")
        return result.get("result", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        raw = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}})
        if "error" in raw:
            return {"ok": False, "text": json.dumps(raw["error"]), "data": None}
        result = raw.get("result", {}) or {}
        parts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        return {"ok": not result.get("isError", False),
                "text": "\n".join(p for p in parts if p),
                "data": result.get("structuredContent")}


def _get_client() -> MCPClient:
    config = _get_config()
    url = config.get("url", "http://localhost:8767/message")
    token = config.get("api_key")
    if not token:
        raise RuntimeError(
            "No token configured. Run `hermes memory setup` or set "
            "PERSEUS_VAULT_MCP_TOKEN.")
    client = MCPClient(url, token)
    init_result = client.initialize()
    if "error" in init_result:
        raise RuntimeError(f"MCP initialize failed: {init_result['error']}")
    return client


def cmd_status(args) -> int:
    try:
        client = _get_client()
        print("Provider: perseus-vault")
        print("Connected: True")
        print(f"  URL: {client.url}")
        tools = client.list_tools()
        print(f"  Tools available: {len(tools)}")
    except Exception as e:
        print("Provider: perseus-vault")
        print("Connected: False")
        print(f"  Error: {e}")
        return 1
    return 0


def cmd_tools(args) -> int:
    try:
        client = _get_client()
        tools = client.list_tools()
        if not tools:
            print("No tools available")
            return 1
        print(f"Available tools ({len(tools)}):")
        for t in tools:
            print(f"  - {t['name']}: {t.get('description', '')[:80]}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_call(args) -> int:
    if not args.tool:
        print("Usage: hermes perseus-vault call <tool_name> [json_args]")
        return 1
    try:
        client = _get_client()
        arguments = {}
        if args.json_args:
            try:
                arguments = json.loads(args.json_args)
            except json.JSONDecodeError as e:
                print(f"Invalid JSON: {e}")
                return 1
        result = client.call_tool(args.tool, arguments)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_recall(args) -> int:
    if not args.query:
        print("Usage: hermes perseus-vault recall <query> [--limit N]")
        return 1
    try:
        client = _get_client()
        result = client.call_tool(
            "perseus_vault_recall",
            {"query": args.query, "limit": args.limit or 10, "preview_cap": 400})
        payload = result.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text", "") or "{}")
            except Exception:
                payload = {}
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                payload = json.loads(inner)
            except Exception:
                pass
        entities = payload.get("entities") or []
        print(f"Found {len(entities)} entities:")
        for e in entities:
            print(f"  - [{e.get('category')}/{e.get('key')}] "
                  f"{str(e.get('summary', e.get('content', '')))[:80]}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_semantic(args) -> int:
    if not args.query:
        print("Usage: hermes perseus-vault semantic <query> [--top_k N]")
        return 1
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_semantic_search",
                                  {"query": args.query, "limit": args.top_k or 10})
        payload = result.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text", "") or "{}")
            except Exception:
                payload = {}
        inner = payload.get("result")
        if isinstance(inner, str):
            try:
                payload = json.loads(inner)
            except Exception:
                pass
        entities = payload.get("entities") or []
        print(f"Found {len(entities)} entities:")
        for e in entities:
            print(f"  - [{e.get('category')}/{e.get('key')}] "
                  f"{str(e.get('summary', e.get('content', '')))[:80]}")
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_remember(args) -> int:
    if not args.category or not args.content:
        print("Usage: hermes perseus-vault remember <category> <content> "
              "[--key KEY] [--type TYPE] [--tags TAGS]")
        return 1
    try:
        client = _get_client()
        config = _get_config()
        call_args = {
            "category": args.category,
            "key": args.key or f"cli-{abs(hash(args.content)) % 100000}",
            "body_json": json.dumps({"summary": args.content}),
            "type": args.type or "insight",
            "tags": args.tags.split(",") if args.tags else [],
        }
        if config.get("workspace_hash"):
            call_args["workspace_hash"] = config["workspace_hash"]
        result = client.call_tool("perseus_vault_remember", call_args)
        print(f"Remembered: {result.get('ok')}")
        print(result.get("text", "")[:400])
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_stats(args) -> int:
    try:
        client = _get_client()
        result = client.call_tool("perseus_vault_stats", {})
        payload = result.get("data")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(result.get("text", "") or "{}")
            except Exception:
                payload = {}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


def cmd_config_show(args) -> int:
    config = _get_config()
    safe = {k: v for k, v in config.items() if k != "api_key"}
    print(json.dumps(safe, indent=2))
    return 0


def register_cli(parser) -> None:
    sub = parser.add_subparsers(dest="pv_cmd", required=True)

    sub.add_parser("status", help="Show provider status").set_defaults(func=cmd_status)
    sub.add_parser("tools", help="List available tools").set_defaults(func=cmd_tools)

    p_call = sub.add_parser("call", help="Call a tool directly")
    p_call.add_argument("tool", help="Tool name")
    p_call.add_argument("json_args", nargs="?", help="JSON arguments")
    p_call.set_defaults(func=cmd_call)

    p_recall = sub.add_parser("recall", help="FTS5 keyword recall")
    p_recall.add_argument("query", help="Search query")
    p_recall.add_argument("--limit", type=int, default=10)
    p_recall.set_defaults(func=cmd_recall)

    p_sem = sub.add_parser("semantic", help="Semantic vector search")
    p_sem.add_argument("query", help="Search query")
    p_sem.add_argument("--top_k", type=int, default=10)
    p_sem.set_defaults(func=cmd_semantic)

    p_rem = sub.add_parser("remember", help="Store an entity")
    p_rem.add_argument("category", help="Category")
    p_rem.add_argument("content", help="Content")
    p_rem.add_argument("--key", help="Stable key")
    p_rem.add_argument("--type", default="insight")
    p_rem.add_argument("--tags", help="Comma-separated tags")
    p_rem.set_defaults(func=cmd_remember)

    sub.add_parser("stats", help="Show Vault statistics").set_defaults(func=cmd_stats)
    sub.add_parser("config", help="Show config").set_defaults(func=cmd_config_show)

    def _dispatch(args):
        if hasattr(args, "func"):
            return args.func(args)
        parser.print_help()
        return 1

    parser.set_defaults(func=_dispatch)
'''


def write_plugin_files(plugin_dir: Path) -> None:
    """Write all plugin files."""
    files = {
        "plugin.yaml": PLUGIN_YAML,
        "__init__.py": INIT_PY,
        "provider.py": PROVIDER_PY,
        "cli.py": CLI_PY,
    }
    for name, content in files.items():
        (plugin_dir / name).write_text(content)
    log_ok("Plugin files created")


def write_env_token(hermes_home: Path, token: str) -> None:
    """Persist the token to $HERMES_HOME/.env (backup first, mode 600)."""
    env_file = hermes_home / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("PERSEUS_VAULT_MCP_TOKEN="):
                log_ok("PERSEUS_VAULT_MCP_TOKEN already set in .env (left as-is)")
                return
        backup = env_file.with_name(".env.bak-%s" % _now_stamp())
        shutil.copy2(env_file, backup)
        log_info(f"Backed up existing .env to {backup.name}")
        with open(env_file, "a") as f:
            f.write(f"\nPERSEUS_VAULT_MCP_TOKEN={token}\n")
    else:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"PERSEUS_VAULT_MCP_TOKEN={token}\n")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass
    log_ok("Token written to .env as PERSEUS_VAULT_MCP_TOKEN")


def _now_stamp() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def main() -> int:
    print("=" * 60)
    print("  Perseus Vault Memory Provider Installer for Hermes")
    print("=" * 60)
    print()

    # Check Hermes
    log_info("Checking Hermes installation...")
    hermes_version = check_hermes()
    if not hermes_version:
        log_error("Hermes not found in PATH. Install Hermes first: "
                  "https://hermes-agent.nousresearch.com")
        return 1
    log_ok(f"Hermes found: {hermes_version}")

    hermes_home = get_hermes_home()
    log_info(f"Hermes home: {hermes_home}")

    # Environment (canonical names first, legacy aliases accepted)
    env_token = (os.environ.get("PERSEUS_VAULT_MCP_TOKEN", "").strip()
                 or os.environ.get("MCP_PERSEUS_VAULT_API_KEY", "").strip())
    env_url = os.environ.get("PERSEUS_VAULT_URL", "").strip()
    env_host_port = os.environ.get("MCP_HOST_PORT", "").strip()
    if not env_url and env_host_port:
        env_url = f"http://{env_host_port}/message"
    workspace = os.environ.get("PERSEUS_VAULT_WORKSPACE", "").strip()

    print()
    print("--- Perseus Vault MCP Server Configuration ---")

    if not sys.stdin.isatty():
        # Non-interactive: token REQUIRED. Never install with a placeholder.
        if not env_token:
            log_error(
                "Non-interactive mode requires PERSEUS_VAULT_MCP_TOKEN "
                "(legacy alias MCP_PERSEUS_VAULT_API_KEY accepted). "
                "Refusing to install with a placeholder token.")
            return 1
        mcp_url = env_url or f"http://localhost:8767/message"
        connection_type = "local" if mcp_url.startswith(
            ("http://localhost", "http://127.0.0.1")) else "remote"
        log_info(f"Non-interactive mode: url={mcp_url}")
        log_info(f"Workspace hash: {workspace or '(global)'}")
    else:
        print("Enter the IP:port of the machine running the Perseus Vault "
              "MCP server")
        print("Example: localhost:8767 or 192.168.1.54:8767")
        print()
        location = prompt_with_default(
            "Is the server local or remote? (local/remote)", "local").lower()
        if location in ("local", "l", "localhost"):
            default_host_port = env_host_port or "localhost:8767"
            mcp_host_port = prompt_with_default("MCP server IP:port",
                                                default_host_port)
            connection_type = "local"
        else:
            default_host_port = env_host_port or "192.168.1.54:8767"
            mcp_host_port = prompt_with_default("MCP server IP:port",
                                                default_host_port)
            connection_type = "remote"
        mcp_url = f"http://{mcp_host_port}/message"
        log_info(f"MCP URL: {mcp_url}")
        if env_token:
            log_info("Token taken from environment (PERSEUS_VAULT_MCP_TOKEN "
                     "or MCP_PERSEUS_VAULT_API_KEY)")
        else:
            mcp_token = prompt_required("MCP bearer token")
            env_token = mcp_token

    # Plugin directory
    plugin_dir = hermes_home / "plugins" / "perseus-vault"
    if plugin_dir.exists() and any(plugin_dir.iterdir()):
        log_warn(f"Existing plugin found at {plugin_dir}; overwriting files")
    plugin_dir.mkdir(parents=True, exist_ok=True)

    write_plugin_files(plugin_dir)

    log_info("Enabling plugin...")
    result = run_cmd(["hermes", "plugins", "enable", "perseus-vault"],
                     capture=False)
    if result.returncode == 0:
        log_ok("Plugin enabled")
    else:
        log_warn("`hermes plugins enable` returned non-zero "
                 "(may already be enabled)")

    write_env_token(hermes_home, env_token)

    config_file = hermes_home / "perseus-vault.json"
    config = {"url": mcp_url}
    if workspace:
        config["workspace_hash"] = workspace
    config_file.write_text(json.dumps(config, indent=2))
    config_file.chmod(0o600)
    log_ok(f"Config saved to {config_file} (mode 600)")

    log_info("Setting memory.provider...")
    result = run_cmd(["hermes", "config", "set", "memory.provider",
                      "perseus-vault"], capture=False)
    if result.returncode == 0:
        log_ok("memory.provider = perseus-vault")
    else:
        log_warn("`hermes config set memory.provider` returned non-zero; "
                 "set it manually: hermes config set memory.provider "
                 "perseus-vault")

    log_info("Verifying installation...")
    result = run_cmd(["hermes", "memory", "status"])
    stdout = result.stdout or ""
    if "perseus-vault" in stdout and "available" in stdout.lower():
        log_ok("Installation verified (provider available)")
    else:
        log_warn("`hermes memory status` did not confirm availability; "
                 "the provider may need a gateway/session restart to pick "
                 "up the new .env token")

    print()
    print("=" * 60)
    print("  Perseus Vault Memory Provider Ready")
    print("=" * 60)
    print()
    print("Commands available:")
    print("  hermes perseus-vault status       # Check connection")
    print("  hermes perseus-vault tools        # List MCP tools")
    print("  hermes perseus-vault recall <q>   # Keyword search")
    print("  hermes perseus-vault semantic <q> # Vector search")
    print("  hermes perseus-vault remember     # Store memory")
    print("  hermes perseus-vault stats        # Vault stats")
    print("  hermes perseus-vault config       # Show config")
    print()
    print("Memory is injected automatically each turn via prefetch().")
    print(f"Connection: {connection_type}")
    print()
    print("Next steps:")
    print("  1. Restart Hermes (or the gateway) so the new .env token is "
          "picked up.")
    print("  2. Optional: `hermes memory setup` to reconfigure interactively.")
    print("  3. Optional: disable built-in memory for a vault-only setup "
          "(memory.memory_enabled / user_profile_enabled in config.yaml); "
          "hybrid mode is supported — built-in writes mirror to the Vault.")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
