# Perseus Vault — Python Client

The official, **dependency-free** Python client for driving a local
[`perseus-vault`](https://github.com/Perseus-Computing-LLC/perseus-vault) binary
over its MCP JSON-RPC 2.0 **stdio** transport (`perseus-vault serve`).

## Why this exists

Every framework integration (LangGraph, CrewAI, AutoGen, PraisonAI, pydantic-ai,
…) otherwise re-implements the same stdio transport — and independently
re-discovers the same concurrency and lifecycle bugs. This client centralizes
and hardens that transport **once**:

- **Reentrant-lock handshake** — `initialize` runs inside `_request`, which
  needs the lock; a plain lock would deadlock.
- **Spawn under the lock** — no concurrent-startup race that leaks children.
- **Deadline-bounded reads with teardown** — a plain `readline()` blocks forever
  if the child accepts stdin but never replies; reads run on a daemon thread
  against a deadline, and on timeout the child is terminated so a later call
  never races a still-blocked reader on a reused stdout.
- **Auto-respawn** — a dead child is replaced on the next call.
- **Normalized results** — `call_tool` unwraps the MCP `content` envelope;
  recall helpers return uniform `{id, text, metadata, score, raw}` dicts.

It is transport-only and framework-agnostic. Integrations become a thin mapping
from their memory API onto these methods.

## Install

```bash
pip install perseus-vault-client
```

You also need the `perseus-vault` binary (single static file, no deps):

```bash
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash
```

## Usage

```python
from perseus_vault_client import VaultClient

with VaultClient(binary="perseus-vault", db_path="./vault.db") as vault:
    # store
    vault.remember("architecture", "use-sqlite", {"content": "SQLite + FTS5 for the index"})

    # hybrid search
    for hit in vault.recall("what database powers the index", limit=3):
        print(hit["score"], hit["text"])

    # enumerate a whole category (paginated under the hood)
    everything = vault.scan("architecture")

    # pre-rendered markdown block for prompt injection
    block = vault.context(query="database choice")

    # soft-delete; True only if the vault actually archived it
    vault.forget("architecture", "use-sqlite")
```

Configuration falls back to environment variables:
`PERSEUS_VAULT_BIN`, `PERSEUS_VAULT_DB`, `PERSEUS_VAULT_ENCRYPTION_KEY`.

Anything not covered by a typed helper is reachable via `call_tool`:

```python
vault.call_tool("perseus_vault_bitemporal", {"category": "decision", "as_of": 1720000000000})
```

## API

| Method | Tool | Notes |
|---|---|---|
| `remember(category, key=None, body=None, *, importance=None, **extra)` | `*_remember` | key auto-generated if omitted |
| `recall(query, *, category=None, limit=10, mode="hybrid", offset=None)` | `*_recall` | empty `query` = enumerate; `mode` = `hybrid`/`fts5`/`dense` |
| `semantic_search(query, *, category=None, limit=10)` | `*_semantic_search` | dense-only |
| `scan(category, *, page_size=100, max_items=None)` | `*_recall` | paginated full-category enumeration |
| `context(query=None, **extra)` | `*_context` | returns markdown string |
| `forget(category, key, *, reason=None)` | `*_forget` | `True` only if archived |
| `prune(category, *, purge_all=False)` | `*_prune` | `purge_all` clears the category |
| `get_entity(id)` / `stats()` / `health()` | `*_get_entity` / `*_stats` / `*_health` | |
| `call_tool(name, arguments)` / `list_tools()` | any | escape hatch |

## License

MIT
