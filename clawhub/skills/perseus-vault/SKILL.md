---
name: perseus_vault-memory
description: Self-hosted persistent memory for OpenClaw agents via Perseus Vault MCP — 30 tools, hybrid search, AES-256 encryption, zero external dependencies
---

# Perseus Vault — Self-Hosted Persistent Agent Memory

## Purpose

This skill connects your OpenClaw agent to Perseus Vault, a self-hosted Rust binary that provides durable, encrypted persistent memory via stdio MCP. No cloud, no API keys, no Docker — just one binary serving 23 memory tools.

**Perseus Vault runs entirely on your machine.** No data leaves your environment. No external service sees your agent's memories. Every stored memory is AES-256-GCM encrypted at rest.

## What Perseus Vault Does

### Full persistent memory lifecycle

Your agent can **remember** facts, decisions, and context; **recall** them across sessions with keyword search; **search** semantically via dense embeddings; and **forget** stale memories. All memory operations are durable and survive OpenClaw restarts.

### Hybrid search

Perseus Vault combines BM25 keyword search (FTS5) with dense vector embeddings via Reciprocal Rank Fusion (RRF). Your agent gets the best of both worlds — exact keyword matches and semantic similarity in a single query.

### Encryption at rest

All stored data is AES-256-GCM encrypted. Even if someone accesses the database file, they can't read your agent's memories without the encryption key.

### Memory lifecycle management

Perseus Vault applies Ebbinghaus decay to memories — rarely-used facts fade and eventually archive. Your agent's context stays sharp without manual cleanup. Run a `perseus_vault_cohere` grooming pass to auto-link related memories, promote frequently-used ones, and archive decayed ones.

### No external dependencies

Perseus Vault is a single Rust binary (~8MB). No Docker, no PostgreSQL, no Redis, no cloud service. Drop it in, start it, connect via stdio MCP. It runs anywhere OpenClaw runs — Linux, macOS, Windows, even a Raspberry Pi.

## Available Tools (23 total)

### Core CRUD
- `perseus_vault_remember` — Store a fact, decision, or observation with category, key, tags, and confidence
- `perseus_vault_recall` — Keyword search across all stored memories with FTS5
- `perseus_vault_get_entity` — Retrieve full details of a specific memory
- `perseus_vault_forget` — Soft-delete a memory (recoverable)

### Semantic search
- `perseus_vault_embed` — Generate and store dense embeddings for vector search
- `perseus_vault_search_memories` — Semantic search via dense embeddings (requires `--llm-endpoint`)
- `perseus_vault_ask` — Ask a natural language question, get a grounded answer with cited sources

### Memory lifecycle
- `perseus_vault_cohere` — Autonomous grooming: promote hot memories, link related ones, archive stale
- `perseus_vault_decay` — Recalculate Ebbinghaus decay scores across all memories
- `perseus_vault_prune` — Bulk archive low-decay or old memories
- `perseus_vault_compact` — Archive memories below a decay threshold

### Knowledge graph
- `perseus_vault_link` — Create relationships between memories
- `perseus_vault_unlink` — Remove stale relationships
- `perseus_vault_traverse` — Walk the relationship graph from any memory

### Journal & timeline
- `perseus_vault_journal` — Append structured decision/observation log entries
- `perseus_vault_timeline` — Query the journal by time range and event type

### Vault (import/export)
- `perseus_vault_vault_export` — Export all memories to Obsidian-compatible .md files
- `perseus_vault_vault_import` — Import .md vault files, idempotent (no duplicates)

### State & proactive recall
- `perseus_vault_state_set` / `perseus_vault_state_get` / `perseus_vault_state_delete` — Key-value state with optional TTL
- `perseus_vault_recall_when` — Proactive just-in-time memory: surfaces relevant memories before tool calls
- `perseus_vault_conflicts` — Detect contradictory or duplicate memories for review

### Monitoring
- `perseus_vault_health` — Health check
- `perseus_vault_stats` — Entity counts by category, database size, date range
- `perseus_vault_context` — Pre-formatted markdown context block for session injection
- `perseus_vault_workspace_list` — List all knowledge domains in the database

## Setup Instructions

### Step 1 — Install Perseus Vault

Choose one:

**Download binary (fastest):**
```bash
# Linux x86_64
curl -L https://github.com/Perseus-Computing-LLC/perseus-vault/releases/latest/download/perseus-vault-x86_64-unknown-linux-gnu -o perseus-vault
chmod +x perseus-vault
sudo mv perseus-vault /usr/local/bin/
```

**Build from source (requires Rust):**
```bash
git clone https://github.com/Perseus-Computing-LLC/perseus-vault.git
cd perseus-vault
cargo build --release
sudo cp target/release/perseus-vault /usr/local/bin/
```

**Install the prebuilt binary (alternative to building from source):**
```bash
curl -sSf https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/install.sh | sh
```

### Step 2 — Configure Perseus Vault as an MCP server in OpenClaw

Add to your OpenClaw MCP servers config:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": ["--db", "~/.openclaw/perseus-vault/perseus-vault.db"],
      "env": {
        "PERSEUS_VAULT_ENCRYPTION_KEY": "${PERSEUS_VAULT_ENCRYPTION_KEY}"
      }
    }
  }
}
```

For semantic search with embeddings, also set:
```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": [
        "--db", "~/.openclaw/perseus-vault/perseus-vault.db",
        "--llm-endpoint", "http://localhost:11434"
      ],
      "env": {
        "PERSEUS_VAULT_ENCRYPTION_KEY": "${PERSEUS_VAULT_ENCRYPTION_KEY}"
      }
    }
  }
}
```

### Step 3 — Generate an encryption key (optional but recommended)

```bash
# Generate a 32-byte key
PERSEUS_VAULT_ENCRYPTION_KEY=$(openssl rand -hex 32)
echo "PERSEUS_VAULT_ENCRYPTION_KEY=$PERSEUS_VAULT_ENCRYPTION_KEY" >> ~/.openclaw/.env
```

Without an encryption key, Perseus Vault stores data unencrypted (still local).

### Step 4 — Initialize and verify

```bash
# Create the database directory
mkdir -p ~/.openclaw/perseus-vault

# Start Perseus Vault once to initialize
perseus-vault --db ~/.openclaw/perseus-vault/perseus-vault.db --health

# Verify it's running
perseus-vault --db ~/.openclaw/perseus-vault/perseus-vault.db --stats
```

Then start a new OpenClaw session. Your agent now has access to all 23 Perseus Vault memory tools.

### Step 5 — Web dashboard (optional)

```bash
# Start the web dashboard on port 8789
perseus-vault --db ~/.openclaw/perseus-vault/perseus-vault.db --dashboard --port 8789
# Open http://localhost:8789
```

## Data Handling & Privacy

Perseus Vault is entirely self-hosted. No data leaves your machine.

- **What gets stored:** Only what your agent explicitly passes to `perseus_vault_remember` or `perseus_vault_journal` tool calls. No automatic capture, no silent monitoring.
- **Where it's stored:** A local SQLite database at the path you specify (`--db`). You control the file.
- **Encryption:** AES-256-GCM at rest when an encryption key is provided. Without a key, data is stored in plaintext SQLite (still local).
- **Who can read it:** Only processes with access to the database file and encryption key. No network access by default.
- **Retention:** Memories decay naturally via Ebbinghaus scoring. You control decay thresholds. Nothing is deleted without your agent's action.
- **No telemetry:** No analytics, no usage tracking, no phone-home. Perseus Vault is a local binary.
- **MIT licensed:** Fully open source. You can audit the code, fork it, embed it.

## Constraints

- **No cloud sync:** Perseus Vault is local-only by design. Use `perseus_vault_vault_export` and git for backup/sharing.
- **Embeddings require Ollama or compatible endpoint:** Semantic search needs `--llm-endpoint` pointing to an Ollama instance or compatible embedding API. Keyword search (FTS5) works without it.
- **Single-writer:** Perseus Vault uses SQLite. One process at a time. Works perfectly for a single-agent OpenClaw setup.

## Complementary Skills

Pair Perseus Vault with these ClawHub skills for a complete memory stack:

- `memory-audit-guardian` — Weekly memory governance audit
- `skill-from-memory` — Extract reusable skills from stored memories
- `knox-governance` — Audit logging of all memory operations

## CI / Automation

To run Perseus Vault in CI or scheduled jobs:
```bash
# Start Perseus Vault in the background
perseus-vault --db /tmp/perseus_vault_ci.db &

# Run a coherence grooming pass nightly
perseus-vault --db ~/.openclaw/perseus-vault/perseus-vault.db --cohere

# Export to vault for git backup
perseus-vault --db ~/.openclaw/perseus-vault/perseus-vault.db --vault-export ~/perseus-vault-vault/
```

## Links

- GitHub: https://github.com/Perseus-Computing-LLC/perseus-vault
- Website: https://perseus.observer/perseus-vault
- Smithery: https://smithery.ai/server/perseus-vault
- mcpservers.org: https://mcpservers.org/servers/perseus-computing-llc/perseus-vault
