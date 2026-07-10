# Perseus Vault Integrations

Ready-to-use adapters that connect Perseus Vault to popular AI agent frameworks.

## Available Integrations

| Framework | Type | Directory |
|---|---|---|
| **Python Client** | Official stdio transport client | [`client/`](client/) |
| **LangGraph** (LangChain) | `BaseStore` implementation | [`langgraph/`](langgraph/) |
| **CrewAI** | Agent Tool | [`crewai/`](crewai/) |
| **AutoGen** (AG2 / autogen-core) | `Memory` implementation | [`autogen/`](autogen/) |
| **FastMCP EventStore** (MCP SDK) | `EventStore` implementation | [`perseus-vault-persist/`](perseus-vault-persist/) |
| **Claude Code** (Anthropic) | MCP server config | [`../docs/integration/claude-code.md`](../docs/integration/claude-code.md) |
| **Cursor** | MCP server config | [`../docs/integration/cursor.md`](../docs/integration/cursor.md) |

## Adding a New Integration

Each integration lives in its own directory with:

```
integrations/<framework>/
├── perseus_vault_<framework>/
│   └── __init__.py     # Main adapter code
├── pyproject.toml       # Package metadata
└── README.md            # Usage guide
```

The adapter pattern:
1. **MCP subprocess call** — Uses Perseus Vault's stdio MCP transport
2. **Framework interface mapping** — Maps the framework's memory API to Perseus Vault tools
3. **Drop-in compatibility** — Works as a replacement for the framework's default memory

## Requirements

All integrations require Perseus Vault v1.0.0+ installed:

```bash
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash
```
