# Mimir vs Zep: Binary vs Infrastructure

## Quick Summary

| | Mimir | Zep |
|---|---|---|
| **Stars** | ~20 | ~3K |
| **Language** | Rust | Go + Python |
| **Deployment** | Single binary (~8MB) | Docker (multiple services) |
| **Dependencies** | Zero (SQLite bundled) | PostgreSQL + Go runtime |
| **MCP Tools** | 36 | 0 (not MCP-native) |
| **Memory Model** | Structured entities + journal + state | Conversation history + facts |
| **Search** | FTS5 + Dense + RRF hybrid | Vector + Graph |
| **Offline** | ✅ Fully local | ❌ Docker + PostgreSQL needed |
| **Encryption** | AES-256-GCM | ❌ |
| **License** | MIT | Apache 2.0 |
| **API** | MCP JSON-RPC (stdio/SSE/HTTP) | REST API |

## Architecture

### Mimir: One Binary

```
Agent ──MCP── mimir ── SQLite (single file)
```

Mimir is a single Rust binary. The database is one file. Install, run, done.

### Zep: Microservices

```
Agent ──REST── Zep API ── PostgreSQL
                  ├── Graph service
                  ├── Search service
                  └── Message store
```

Zep requires Docker Compose with multiple services. Even the "light" version
needs PostgreSQL.

## MCP-Native vs REST

Mimir is built on MCP from the ground up. The binary IS an MCP server:

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

Zep uses a REST API. Connecting to MCP hosts requires writing an MCP wrapper
server. This adds complexity and another point of failure.

## Memory Models

### Mimir: Entity-First

Mimir's memory model is built around structured entities:
- Idempotent by `(category, key)`
- Lifecycle: buffer → working → core → archived
- Graph relationships between entities
- Journal for audit trail
- State management with TTL
- Always-on entities for session injection

### Zep: Conversation-First

Zep is built around conversation history:
- Message storage and retrieval
- Fact extraction from conversations
- User/session-based organization
- Graph-based knowledge representation

Zep excels at conversation-heavy use cases. Mimir excels at structured
knowledge management across any agent workflow.

## MCP Tools: 36 vs 0

This is the biggest gap. Mimir has 36 MCP tools. Zep has zero — it's not
an MCP server at all. To use Zep with MCP hosts, you need to write a bridge.

Mimir's tools cover:
- CRUD operations on entities
- Hybrid search (keyword + vector)
- Graph traversal and linking
- Journal and timeline
- State management with TTL
- Lifecycle management (decay, prune, purge)
- Quality scoring and conflict detection
- Vault export/import
- Federation across workspaces
- RAG and embedding generation
- Performance benchmarking

## When to Use Mimir

- You want **zero infrastructure** memory
- You're building with MCP hosts (Claude Desktop, Cursor, Hermes)
- You need **encryption at rest**
- You want a structured entity model with lifecycle management
- You're deploying to edge/air-gapped environments
- You need the full suite of memory operations (not just CRUD)

## When to Use Zep

- You're building a **conversation-heavy application** (chatbots, support)
- You need **user/session-based** memory organization
- Your team already runs PostgreSQL and Docker
- You prefer REST APIs over MCP
- You need Zep's conversation summarization and fact extraction features

## Honest Assessment

**Mimir's strengths vs Zep:**
- Zero infrastructure (single binary vs Docker + PostgreSQL)
- MCP-native (no wrapper needed)
- 36 tools vs 0 MCP tools
- Encryption at rest
- Full entity lifecycle
- Single-file database (easy backup/restore)

**Mimir's weaknesses vs Zep:**
- Zep has stronger conversation-history features (summarization, entity extraction)
- Zep's graph-based knowledge representation is more sophisticated
- Zep has a managed cloud offering (Zep Cloud)
- Larger existing user base
- REST API is more familiar to web developers

**If you're choosing today:** Mimir wins on simplicity (one binary, no Docker)
and MCP integration. Zep wins if you need advanced conversation processing and
already have PostgreSQL infrastructure.
