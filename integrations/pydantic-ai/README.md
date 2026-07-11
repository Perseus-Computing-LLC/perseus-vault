# Perseus Vault — Pydantic AI Integration

Give a [Pydantic AI](https://ai.pydantic.dev) agent persistent, **local-first**
memory backed by [Perseus Vault](https://github.com/Perseus-Computing-LLC/perseus-vault)
— a single static binary (SQLite + FTS5 + bundled ONNX embeddings, optional
AES-256-GCM), no external service, works offline.

## How it works

Perseus Vault speaks MCP JSON-RPC over stdio, and Pydantic AI has first-class
MCP support, so this integration is a thin, idiomatic wrapper: it builds a
`StdioTransport` → `MCPToolset` → `MCP` **capability**. The agent discovers the
vault's tools (`perseus_vault_remember`, `perseus_vault_recall`,
`perseus_vault_semantic_search`, … — 55+ tools) and calls them like any other
tool, with Pydantic AI's tracing, caching, and lifecycle handling intact.

> Design note: this uses Pydantic AI's own MCP machinery rather than a custom
> transport. On this framework that's the correct, lowest-surface approach —
> the maintainers explicitly steer cross-run memory toward MCP + the
> capabilities/harness layer rather than a memory abstraction in core.

## Install

```bash
pip install perseus-vault-pydantic-ai
```

Plus the `perseus-vault` binary (single static file, no deps):

```bash
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash
```

## Usage

```python
from pydantic_ai import Agent
from perseus_vault_pydantic_ai import perseus_vault_capability

memory = perseus_vault_capability(binary="perseus-vault", db_path="./agent.db")

agent = Agent("openai:gpt-5", capabilities=[memory])

async def main():
    async with agent:
        await agent.run("Remember that I prefer metric units.")
        # ... later, even in a new process pointed at the same db_path:
        result = await agent.run("What units do I prefer?")
        print(result.output)
```

Config falls back to environment variables: `PERSEUS_VAULT_BIN`,
`PERSEUS_VAULT_DB`, `PERSEUS_VAULT_ENCRYPTION_KEY`.

### Toolset form

If you'd rather pass a toolset directly instead of a capability:

```python
from pydantic_ai import Agent
from perseus_vault_pydantic_ai import perseus_vault_toolset

agent = Agent("openai:gpt-5", toolsets=[perseus_vault_toolset(db_path="./agent.db")])
```

### Restricting the tool surface

```python
memory = perseus_vault_capability(
    db_path="./agent.db",
    allowed_tools=["perseus_vault_remember", "perseus_vault_recall"],
)
```

## API

| Function | Returns | Use |
|---|---|---|
| `perseus_vault_capability(...)` | `pydantic_ai.capabilities.MCP` | pass via `Agent(capabilities=[...])` |
| `perseus_vault_toolset(...)` | `pydantic_ai.mcp.MCPToolset` | pass via `Agent(toolsets=[...])` |

Both accept: `binary`, `db_path`, `encryption_key`, `env`, `extra_args`
(+ `id`, `allowed_tools`, and passthrough `**mcp_kwargs` on the capability).

## License

MIT
