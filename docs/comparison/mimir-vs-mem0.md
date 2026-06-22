# Mimir vs Mem0: Local-First vs Cloud-Only Memory

## Quick Summary

| | Mimir | Mem0 |
|---|---|---|
| **Stars** | ~20 | ~55K |
| **Language** | Rust | Python |
| **Deployment** | Single binary (~8MB) | Cloud API or self-host (Python + vector DB) |
| **Dependencies** | Zero (SQLite bundled) | Python runtime + PostgreSQL/Qdrant/Neo4j |
| **MCP-Native** | ✅ 36 tools, full MCP | ❌ Not MCP-native |
| **Offline/Local** | ✅ Fully local, no network | ❌ Cloud-dependent; self-host needs infra |
| **Encryption** | AES-256-GCM at rest | ❌ |
| **Search** | FTS5 + Dense + RRF hybrid | Vector only |
| **Memory Model** | Structured entities with journal | Flat memory entries |
| **License** | MIT | Apache 2.0 |
| **Embeddings** | Optional (Ollama or OpenAI-compatible) | Required (OpenAI default) |

## When to Use Mimir

- You want a **single binary** with no infrastructure
- You need **fully offline** operation (air-gapped, classified environments)
- You work with **MCP hosts** (Claude Desktop, Cursor, Hermes Agent)
- You need **encryption at rest** for sensitive data
- You want **hybrid search** (keyword + vector) out of the box
- You're deploying to **edge devices or CI runners**
- You want to **own your data** — no cloud dependency

## When to Use Mem0

- Your team already uses Mem0 and needs continuity
- You need a **managed cloud service** with zero ops
- You want a larger ecosystem with more community examples
- You're building for production at scale with existing Mem0 infra
- You prefer Python-native libraries over subprocess-based tool calls

## Architecture Differences

### Mimir: Single Binary, No Dependencies

```
Agent ──MCP stdio── mimir (Rust binary)
                      ├── SQLite (entities, journal, state)
                      ├── FTS5 (keyword search)
                      ├── Dense vectors (optional Ollama/OpenAI)
                      └── Web dashboard (optional)
```

Mimir is a self-contained Rust binary. The SQLite database is a single file.
Install is one `curl | sh`. Nothing else to configure.

### Mem0: Python + External Services

```
Agent ──HTTP── mem0 Python library
                ├── PostgreSQL (or Qdrant, Neo4j, etc.)
                ├── Embedding service (OpenAI, etc.)
                └── Python runtime
```

Mem0 requires a Python environment and at least one external database.
The "self-hosted" path still needs PostgreSQL, Qdrant, or Neo4j running.

## Memory Models

### Mimir: Structured Entities

Entities in Mimir have explicit structure:
- **Category + Key**: Namespaced, idempotent storage
- **Type**: Declared entity type (fact, decision, preference, etc.)
- **Decay**: Ebbinghaus decay with retrieval boosts
- **Links**: Graph relationships between entities
- **Journal**: Append-only event log with actor attribution
- **State**: Key-value with TTL
- **Visibility**: Public, private, workspace-scoped

This structure enables features Mem0 can't match:
- Automatic decay of unused memories
- Conflict detection via trigram similarity
- Entity linking and graph traversal
- Session context injection with always-on entities

### Mem0: Flat Memory Entries

Mem0 stores flat memory entries with metadata. No structured entity model,
no decay lifecycle, no journal, no state management. It's a simpler model
that works well for straightforward RAG use cases.

## MCP Tools: 36 vs 5

Mimir exposes 36 MCP tools covering the full memory lifecycle:

| Category | Mimir Tools |
|---|---|
| CRUD | remember, recall, recall_when, get_entity, forget |
| Graph | link, unlink, traverse |
| Journal | journal, timeline |
| State | state_set, state_get, state_delete, state_list |
| Lifecycle | decay, prune, purge, cohere, compact, reindex |
| Quality | score, conflicts, correct |
| RAG | ask, embed, context |
| Vault | vault_export, vault_import |
| Federation | federate, workspace_list |
| Metrics | stats, health, bench, synthesize |
| Connectors | ingest |

Mem0 exposes ~5 tools: add, search, get, get_all, delete.

## MCP-Native Advantage

Mimir is MCP-native — the binary IS an MCP server. Connect via stdio, SSE, or HTTP:

```json
{
  "mcpServers": {
    "mimir": {
      "command": "mimir",
      "args": ["serve", "--db", "~/.mimir/data/mimir.db"]
    }
  }
}
```

Mem0 requires a wrapper to expose MCP tools. The MCP integration is an
afterthought, not the native interface.

## Honest Assessment

**Mimir's strengths:**
- Zero-dependency deployment (one binary, one file)
- MCP-native architecture
- Structured memory model with lifecycle management
- Encryption at rest
- Fully offline operation
- MIT license

**Mimir's weaknesses vs Mem0:**
- Much smaller community (20 stars vs 55K)
- Requires Rust toolchain to build from source
- No managed cloud offering
- Fewer tutorials and integration examples
- Binary distribution is less familiar to Python developers

**If you're choosing today:** Pick Mimir if you value simplicity, privacy, and
MCP-native design. Pick Mem0 if you need cloud-managed memory or already have
a Python/PostgreSQL stack.
