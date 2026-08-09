# Perseus Vault — Hermes Native Memory Provider Plugin

> **This is a native Hermes MemoryProvider plugin**, not an MCP tool integration.
> It implements the `MemoryProvider` ABC from Hermes core, enabling **automatic lifecycle hooks** (SessionStart, on_insight, SessionStop) that the agent calls without manual tool invocation.

## Repository Structure

This integration lives in the Perseus Vault repo at:
```
perseus-vault/
├── integrations/
│   └── hermes/                    # ← THIS INTEGRATION
│       ├── install-perseus-vault.py   # One-file installer
│       └── README.md                # This file
```

## Overview

| Aspect | Old (MCP Tools) | New (Native Plugin) |
|--------|-----------------|---------------------|
| **Integration type** | Manual MCP tool calls | Native `MemoryProvider` plugin |
| **Lifecycle** | Agent must remember to call tools | **Automatic** via `on_turn_start`, `on_memory_write`, `on_session_end` |
| **Memory injection** | Manual `perseus_vault_context` | Injected into system prompt each turn |
| **Capture** | Manual `perseus_vault_remember` | Auto-captures on insight + session consolidation |
| **Setup** | `hermes mcp add perseus-vault` | `hermes memory setup perseus-vault` |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        HERMES AGENT                         │
├─────────────────────────────────────────────────────────────┤
│  MemoryManager                                              │
│    ├── on_turn_start  ──▶ SessionStart hook                │
│    ├── on_memory_write ──▶ on_insight hook                 │
│    └── on_session_end  ──▶ SessionStop hook                │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              PERSEUS VAULT MEMORY PROVIDER                  │
│  (~/.hermes/plugins/perseus-vault/)                         │
├─────────────────────────────────────────────────────────────┤
│  plugin.yaml    # Plugin manifest                          │
│  __init__.py    # register(ctx) → MemoryProvider           │
│  provider.py    # PerseusVaultProvider (implements hooks)  │
│  cli.py         # hermes perseus-vault <cmd>               │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP/JSON-RPC (MCP protocol)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              PERSEUS VAULT MCP SERVER                       │
│  (Your server: ip-server:port)                              │
├─────────────────────────────────────────────────────────────┤
│  • 81 MCP tools (recall, remember, semantic_search, etc.)  │
│  • SQLite + FTS5 + vector embeddings                        │
│  • Bi-temporal queries, decay, consolidation                │
└─────────────────────────────────────────────────────────────┘
```

## Lifecycle Hooks (per `docs/lifecycle-hooks.md`)

### 1. SessionStart — `on_turn_start(turn=1, message)`
**Runs before first substantive action.** Seeds session with relevant memories.

```python
# Hook calls THREE MCP tools:
1. perseus_vault_context(query=message, mode="on_demand", limit=10)
2. perseus_vault_recall(query=message, limit=10)          # keyword search
3. perseus_vault_recall_when(context=message, limit=5)    # trigger matching (turns 2+)
```

### 2. on_insight — `on_memory_write(action, target, content, metadata)`
**Runs mid-session** when agent learns something durable. Dispatches to appropriate tool:

| Action/Type | MCP Tool | Purpose |
|-------------|----------|---------|
| `remember`/`fact`/`decision`/`lesson` | `perseus_vault_remember` | Durable facts with `recall_when` |
| `event`/`journal` | `perseus_vault_journal` | Significant events |
| `capture`/`raw` | `perseus_vault_capture` | Distill raw payload |

### 3. SessionStop — `on_session_end(messages)`
**Runs before finishing.** Consolidates session memories into durable observations.

```python
# Two-step consolidation:
1. perseus_vault_consolidate(dry_run=True)     # preview
2. perseus_vault_consolidate(dry_run=False, archive_sources=True)  # merge
```
*Separate from cron's nightly `perseus-vault maintain` (decay + compact + vacuum).*

## Quick Install

### One-liner (non-interactive)
```bash
MCP_HOST_PORT=ip-server:port MCP_PERSEUS_VAULT_API_KEY=mcp_perseus-vault_token \
  curl -fsSL https://raw.githubusercontent.com/sowerkoku/perseus-vault/main/integrations/hermes/install-perseus-vault.py | python3
```

### Interactive
```bash
curl -fsSL https://raw.githubusercontent.com/sowerkoku/perseus-vault/main/integrations/hermes/install-perseus-vault.py > install-perseus-vault.py

python3 install-perseus-vault.py
# Prompts: local/remote → IP:port → token
```

### Environment Variables (for automation)
| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_HOST_PORT` | Perseus Vault server `host:port` | `localhost:8767` |
| `MCP_PERSEUS_VAULT_API_KEY` | MCP Bearer token | *(required)* |
| `HERMES_HOME` | Hermes config directory | `~/.hermes` |

## Post-Install Verification

```bash
# Memory provider status
hermes memory status
# → Provider: perseus-vault | Status: available ✓

# Plugin connection
hermes perseus-vault status
# → Connected: True | URL: http://ip-server:port/message | Tools: 81

# List available tools
hermes perseus-vault tools
```

## ⚠️ Important: Disable Built-in Memory

The installer enables the Perseus Vault provider, but Hermes also has a built-in memory system (MEMORY.md/USER.md) that runs in parallel. To avoid **duplicate memory injection** and conflicts, **disable the built-in memory** after installation:

```bash
# Edit ~/.hermes/config.yaml
# Find the memory: section and set both to false:
memory:
  memory_enabled: false
  user_profile_enabled: false
```

Then verify:
```bash
hermes memory status
# Should show:
#   Memory injection:   disabled ✗
#   User profile:       disabled ✗
#   Memory tool:        enabled ✓
#   Provider:  perseus-vault | Status: available ✓
```

## Usage in Session

The provider is **automatic** — no manual tool calls needed:

```
User: "What did we decide about the cache layer?"
→ Agent automatically has relevant context injected via SessionStart hook
→ Agent answers from seeded memory (no manual recall needed)
```

When the agent learns something:
```
User: "Remember: we chose SQLite WAL mode for the cache layer"
→ Agent calls on_memory_write → perseus_vault_remember with recall_when
```

At session end:
```
→ on_session_end runs perseus_vault_consolidate (dry_run + archive)
```

## CLI Commands (optional, for debugging)

```bash
hermes perseus-vault status          # Connection health
hermes perseus-vault tools           # List 81 MCP tools
hermes perseus-vault recall "query"  # FTS5 keyword search
hermes perseus-vault semantic "query" # Vector search
hermes perseus-vault remember        # Manual store
hermes perseus-vault stats           # Vault statistics
hermes perseus-vault config          # Show config (non-secret)
```

## Cron (Server-side Hygiene)

The **server** runs nightly maintenance (separate from agent's SessionStop):

```bash
# Nightly: cohere → decay → compact → consolidate → dedup/orphans/reindex
15 3 * * *  perseus-vault maintain --db /abs/path/perseus-vault.db

# Weekly: + vacuum
30 3 * * 0  perseus-vault maintain --db /abs/path/perseus-vault.db --vacuum
```

## Requirements

- **Hermes Agent** ≥ 0.20.0 (native plugin support)
- **Perseus Vault MCP Server** ≥ 2.22.0 running at `host:port`
- **Python** 3.10+ (for installer)

## Plugin Files

| File | Purpose |
|------|---------|
| `plugin.yaml` | Plugin manifest (name, version, config schema, entry_point) |
| `__init__.py` | `register(ctx)` → returns `create_provider()` |
| `provider.py` | `PerseusVaultProvider` implementing `MemoryProvider` ABC + 3 lifecycle hooks |
| `cli.py` | `hermes perseus-vault <cmd>` subcommands |

## References

- **Hermes MemoryProvider Plugin Spec**: https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin
- **Perseus Vault Lifecycle Hooks**: https://github.com/sowerkoku/perseus-vault/blob/main/docs/lifecycle-hooks.md
- **Perseus Vault MCP Tools**: https://github.com/sowerkoku/perseus-vault/blob/main/docs/tools-reference.md

## License

MIT — Same as Perseus Vault.