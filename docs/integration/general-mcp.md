# General MCP Integration Guide

Perseus Vault is an MCP stdio server. It works with **any** MCP-compatible client.

## Bootstrap (60 seconds)

```bash
# Install Perseus Vault
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash

# Create data directory
mkdir -p ~/.mimir/data

# Verify it works
/usr/local/bin/perseus-vault --version
```

## MCP Client Configuration

All MCP clients use the same pattern. The exact config format varies by client:

### stdio transport (universal)

```yaml
# Generic config
command: /usr/local/bin/perseus-vault
args:
  - "--db"
  - "~/.mimir/data/perseus-vault.db"
```

### Client-specific formats

| Client | Config file | Format |
|---|---|---|
| **Claude Code** | `claude mcp add` | CLI command (see [guide](claude-code.md)) |
| **Cursor** | `.cursor/mcp.json` | JSON (see [guide](cursor.md)) |
| **Codex** | `.codex/mcp.json` or `~/.codex/mcp.json` | JSON |
| **Hermes Agent** | `config.yaml` | YAML |
| **Continue** | `~/.continue/config.json` | JSON |
| **Cline** | VS Code settings | JSON |
| **Roo Code** | `.roomodes` | JSON |

### Hermes Agent config

```yaml
mcp_servers:
  perseus-vault:
    command: "/usr/local/bin/perseus-vault"
    args: ["--db", "/home/YOUR_USER/.mimir/data/perseus-vault.db"]
    timeout: 60
    connect_timeout: 30
```

### Codex config

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "/usr/local/bin/perseus-vault",
      "args": ["--db", "~/.mimir/data/perseus-vault.db"]
    }
  }
}
```

### Continue config

```json
{
  "experimental": {
    "mcpServers": {
      "perseus-vault": {
        "command": "/usr/local/bin/perseus-vault",
        "args": ["--db", "~/.mimir/data/perseus-vault.db"]
      }
    }
  }
}
```

## Tools (90 canonical)

Perseus Vault exposes **90 canonical MCP tools** under the `perseus_vault_*`
prefix (legacy `mimir_*` / `mneme_*` aliases remain callable). A representative
selection is shown below; run `perseus-vault --version` and your client's tool
list to see all of them.

| Category | Tools |
|---|---|
| **CRUD** | `mimir_remember`, `mimir_recall`, `mimir_forget`, `mimir_get_entity`, `mimir_recall_when` |
| **Graph** | `mimir_link`, `mimir_unlink`, `mimir_traverse` |
| **Journal** | `mimir_journal`, `mimir_timeline` |
| **State** | `mimir_state_set`, `mimir_state_get`, `mimir_state_delete`, `mimir_state_list` |
| **AI** | `mimir_ask` (RAG), `mimir_embed` (embeddings), `mimir_cohere` (synthesis) |
| **Connectors** | `mimir_ingest` (GitHub issues, file watcher) |
| **Lifecycle** | `mimir_decay`, `mimir_prune`, `mimir_compact`, `mimir_score` |
| **Quality** | `mimir_conflicts` |
| **Vault** | `mimir_vault_export`, `mimir_vault_import` |
| **Ops** | `mimir_health`, `mimir_stats`, `mimir_migrate`, `mimir_context`, `mimir_workspace_list` |

## Encryption

Perseus Vault supports AES-256-GCM encryption at rest for `body_json` and it is
**enabled by default for fresh installs** — the first write generates
`~/.perseus-vault/secret.key` and establishes the encrypted canary. Explicit
`--encryption-key` paths remain supported:

```bash
# Explicit key (optional; a standard key is auto-generated for fresh installs)
perseus-vault keygen --key-file ~/.perseus-vault/secret.key

# Use with any client (add --encryption-key to args)
/usr/local/bin/perseus-vault --db ~/.mimir/data/perseus-vault.db --encryption-key ~/.perseus-vault/secret.key
```

Existing plaintext databases fail closed with an actionable `init --rekey`
migration path unless `PERSEUS_VAULT_ALLOW_PLAINTEXT=1` is set explicitly.
Note: encryption covers `body_json`; the FTS5 index and metadata stay
plaintext by design (see [docs/ENCRYPTION.md](../ENCRYPTION.md)).

## Docker

```bash
docker run -v ~/.mimir/data:/data ghcr.io/Perseus-Computing-LLC/perseus-vault:latest --db /data/perseus-vault.db
```

## What Perseus Vault Is Not

- ❌ Not a vector database — it's a persistent memory engine
- ❌ Not a cloud service — everything runs locally
- ❌ Not tied to any AI framework — works with any MCP client
- ❌ Not an embedding endpoint — uses Ollama for embeddings (optional)

## Design Philosophy

> Perseus Vault is memory for machines. It remembers what your agents learn so they don't start cold every session. Everything is stored locally, searchable via FTS5 + hybrid search, and exportable as plain Markdown files. No API keys, no cloud dependencies, no vendor lock-in.
