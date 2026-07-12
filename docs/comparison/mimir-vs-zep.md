# Perseus Vault vs Zep: Binary vs Infrastructure

## Quick Summary

| | Perseus Vault | Zep |
|---|---|---|
| **Stars** | ~20 | ~3K |
| **Language** | Rust | Go + Python |
| **Deployment** | Single binary (~8MB) | Docker (multiple services) |
| **Dependencies** | Zero (SQLite bundled) | Neo4j + Go runtime (Graphiti) |
| **Self-hosted server** | ✅ (the binary is the server) | ⚠️ Community Edition deprecated — memory API is Zep Cloud-only |
| **MCP Tools** | 36 | 0 (not MCP-native) |
| **Memory Model** | Structured entities + journal + state | Conversation history + temporal knowledge graph |
| **Search** | FTS5 + Dense + RRF hybrid | Vector + Graph |
| **Offline** | ✅ Fully local | ❌ Docker + Neo4j needed |

## Measured: same-box recall (fully local)

Both systems run on one H100 against the same local Ollama (`qwen2.5:14b-instruct` +
`nomic-embed-text`), identical fact set / queries / substring judge:

| System | Recall | p50 |
|---|---|---|
| **Perseus Vault** (hybrid) | **1.00** | 35.6 ms |
| Zep (Graphiti temporal KG + Neo4j) | 0.20 | 49.7 ms |

Zep's self-hosted Community Edition server is deprecated ([getzep/zep](https://github.com/getzep/zep):
"no longer supported", code moved to `legacy/`) and the `zep_python` v2 memory API is
now **Zep Cloud-only**. So the number above measures Zep's real OSS engine — **Graphiti**,
a temporal knowledge graph on Neo4j — with entity/edge extraction *and* embeddings both
on the same local Ollama. The honest caveat: building a KG requires an LLM to do
structured extraction, and a **local** model is lossy at it (5 entities / 2 edges from 6
facts here), so 0.20 reflects local-extraction quality, **not** Zep Cloud (which uses
frontier models). This is the real cost of running Zep's graph approach air-gapped.
Full artifact: [`benchmark/lambda/results/competitors.json`](../../benchmark/lambda/results/competitors.json).
| **Encryption** | AES-256-GCM | ❌ |
| **License** | MIT | Apache 2.0 |
| **API** | MCP JSON-RPC (stdio/SSE/HTTP) | REST API |

## Architecture

### Perseus Vault: One Binary

```
Agent ──MCP── mimir ── SQLite (single file)
```

Perseus Vault is a single Rust binary. The database is one file. Install, run, done.

### Zep: Microservices

```
Agent ──REST── Zep API ── Neo4j (Graphiti temporal KG)
                  ├── Graph service
                  ├── Search service
                  └── Message store
```

Zep requires Docker Compose with multiple services (the engine is Graphiti on
Neo4j). Its self-hosted Community Edition server is deprecated — the `zep_python`
memory API now targets Zep Cloud, so the fully-local path is Graphiti + Neo4j.

## MCP-Native vs REST

Perseus Vault is built on MCP from the ground up. The binary IS an MCP server:

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

### Perseus Vault: Entity-First

Perseus Vault's memory model is built around structured entities:
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

Zep excels at conversation-heavy use cases. Perseus Vault excels at structured
knowledge management across any agent workflow.

## MCP Tools: 36 vs 0

This is the biggest gap. Perseus Vault has 36 MCP tools. Zep has zero — it's not
an MCP server at all. To use Zep with MCP hosts, you need to write a bridge.

Perseus Vault's tools cover:
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

## When to Use Perseus Vault

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

**Perseus Vault's strengths vs Zep:**
- Zero infrastructure (single binary vs Docker + PostgreSQL)
- MCP-native (no wrapper needed)
- 36 tools vs 0 MCP tools
- Encryption at rest
- Full entity lifecycle
- Single-file database (easy backup/restore)

**Perseus Vault's weaknesses vs Zep:**
- Zep has stronger conversation-history features (summarization, entity extraction)
- Zep's graph-based knowledge representation is more sophisticated
- Zep has a managed cloud offering (Zep Cloud)
- Larger existing user base
- REST API is more familiar to web developers

**If you're choosing today:** Perseus Vault wins on simplicity (one binary, no Docker)
and MCP integration. Zep wins if you need advanced conversation processing and
already have PostgreSQL infrastructure.
