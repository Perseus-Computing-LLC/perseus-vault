# General MCP Integration Guide

Perseus Vault is an MCP stdio server. It works with **any** MCP-compatible client.

## Bootstrap (60 seconds)

```bash
# Install Perseus Vault
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash

# Create data directory
mkdir -p ~/.perseus-vault/data

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
  - "~/.perseus-vault/data/perseus-vault.db"
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
    args: ["--db", "/home/YOUR_USER/.perseus-vault/data/perseus-vault.db"]
    timeout: 60
    connect_timeout: 30
```

### Codex config

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "/usr/local/bin/perseus-vault",
      "args": ["--db", "~/.perseus-vault/data/perseus-vault.db"]
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
        "args": ["--db", "~/.perseus-vault/data/perseus-vault.db"]
      }
    }
  }
}
```

## Tools (99 canonical)

Perseus Vault exposes **99 canonical MCP tools** under the `perseus_vault_*`
prefix (legacy `perseus_vault_*` / `perseus_vault_*` aliases remain callable). A representative
selection is shown below; run `perseus-vault --version` and your client's tool
list to see all of them.

| Category | Tools |
|---|---|
| **CRUD** | `perseus_vault_remember`, `perseus_vault_recall`, `perseus_vault_forget`, `perseus_vault_get_entity`, `perseus_vault_recall_when` |
| **Graph** | `perseus_vault_link`, `perseus_vault_unlink`, `perseus_vault_traverse` |
| **Journal** | `perseus_vault_journal`, `perseus_vault_timeline` |
| **State** | `perseus_vault_state_set`, `perseus_vault_state_get`, `perseus_vault_state_delete`, `perseus_vault_state_list` |
| **AI** | `perseus_vault_ask` (RAG), `perseus_vault_embed` (embeddings), `perseus_vault_cohere` (synthesis) |
| **Connectors** | `perseus_vault_ingest` (GitHub issues, file watcher) |
| **Lifecycle** | `perseus_vault_decay`, `perseus_vault_prune`, `perseus_vault_compact`, `perseus_vault_score` |
| **Quality** | `perseus_vault_conflicts` |
| **Vault** | `perseus_vault_vault_export`, `perseus_vault_vault_import` |
| **Ops** | `perseus_vault_health`, `perseus_vault_stats`, `perseus_vault_migrate`, `perseus_vault_context`, `perseus_vault_workspace_list` |

## Encryption

Perseus Vault supports AES-256-GCM encryption at rest for `body_json` and it is
**enabled by default for fresh installs** — the first write generates
`~/.perseus-vault/secret.key` and establishes the encrypted canary. Explicit
`--encryption-key` paths remain supported:

```bash
# Explicit key (optional; a standard key is auto-generated for fresh installs)
perseus-vault keygen --key-file ~/.perseus-vault/secret.key

# Use with any client (add --encryption-key to args)
/usr/local/bin/perseus-vault --db ~/.perseus-vault/data/perseus-vault.db --encryption-key ~/.perseus-vault/secret.key
```

Existing plaintext databases fail closed with an actionable `init --rekey`
migration path unless `PERSEUS_VAULT_ALLOW_PLAINTEXT=1` is set explicitly.
Note: encryption covers `body_json`; the FTS5 index and metadata stay
plaintext by design (see [docs/ENCRYPTION.md](../ENCRYPTION.md)).

## Docker

```bash
docker run -v ~/.perseus-vault/data:/data ghcr.io/Perseus-Computing-LLC/perseus-vault:latest --db /data/perseus-vault.db
```

## What Perseus Vault Is Not

- ❌ Not a vector database — it's a persistent memory engine
- ❌ Not a cloud service — everything runs locally
- ❌ Not tied to any AI framework — works with any MCP client
- ❌ Not an embedding endpoint — uses Ollama for embeddings (optional)

## Design Philosophy

> Perseus Vault is memory for machines. It remembers what your agents learn so they don't start cold every session. Everything is stored locally, searchable via FTS5 + hybrid search, and exportable as plain Markdown files. No API keys, no cloud dependencies, no vendor lock-in.
