# Awesome Perseus Vault

> Curated list of Perseus Vault integrations, tools, and resources.
> Perseus Vault is an MCP-native, local-first persistent memory engine for AI agents.

## Contents

- [Official Resources](#official-resources)
- [Framework Integrations](#framework-integrations)
- [MCP Hosts](#mcp-hosts)
- [Tools & Plugins](#tools--plugins)
- [Community Projects](#community-projects)
- [Articles & Tutorials](#articles--tutorials)
- [Comparisons](#comparisons)

## Official Resources

- [Perseus Vault GitHub Repo](https://github.com/Perseus-Computing-LLC/perseus-vault) — The Perseus Vault source
- [Roadmap](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/ROADMAP.md)
- [Contributing Guide](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/CONTRIBUTING.md)
- [Security Policy](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/SECURITY.md)

## Framework Integrations

Perseus Vault adapters for popular AI agent frameworks:

### LangGraph (LangChain)
- [mimir-langgraph](https://github.com/Perseus-Computing-LLC/perseus-vault/tree/main/integrations/langgraph) — `MimirStore` implementing `BaseStore`
- Drop-in persistent memory for LangGraph agents
- `pip install -e integrations/langgraph/`

### CrewAI
- [mimir-crewai](https://github.com/Perseus-Computing-LLC/perseus-vault/tree/main/integrations/crewai) — `MimirMemoryTool` as a CrewAI agent tool
- Agents can remember, recall, journal, and get context
- `pip install -e integrations/crewai/`

### AutoGen (AG2 / autogen-core)
- [mimir-autogen](https://github.com/Perseus-Computing-LLC/perseus-vault/tree/main/integrations/autogen) — `MimirMemory` implementing `autogen_core.memory.Memory`
- Context injection before each inference turn
- `pip install -e integrations/autogen/`

### Other Frameworks
Perseus Vault is MCP-native — any framework with MCP support can use Perseus Vault directly:
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) — via MCP stdio
- [Google ADK](https://github.com/google/adk-python) — via MCP stdio
- [Agno](https://github.com/agno-agi/agno) — via MCP stdio
- [Magentic-One](https://github.com/anthropics/anthropic-quickstarts) — via MCP stdio

## MCP Hosts

Perseus Vault works with any MCP host. Configuration is one line:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": ["serve", "--db", "~/.mimir/data/perseus-vault.db"]
    }
  }
}
```

Tested and confirmed working with:
- [Claude Desktop](https://claude.ai/download) — [config guide](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/integration/claude-code.md)
- [Cursor](https://cursor.com) — [config guide](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/integration/cursor.md)
- [Hermes Agent](https://github.com/nousresearch/hermes-agent)
- [Perseus](https://perseus.observer) — native integration
- [OpenClaw](https://openclaw.ai)
- Any host supporting MCP JSON-RPC 2.0 stdio

## Tools & Plugins

### Perseus Vault Itself (57 MCP Tools)

| Category | Tools |
|---|---|
| **Entity CRUD** | `mimir_remember`, `mimir_recall`, `mimir_recall_when`, `mimir_get_entity`, `mimir_capture`, `mimir_forget` |
| **Graph** | `mimir_link`, `mimir_unlink`, `mimir_traverse` |
| **Journal** | `mimir_journal`, `mimir_check_failure_pattern`, `mimir_timeline` |
| **State** | `mimir_state_set`, `mimir_state_get`, `mimir_state_delete`, `mimir_state_list` |
| **Search & RAG** | `mimir_ask`, `mimir_embed`, `mimir_context`, `mimir_ingest` |
| **Lifecycle** | `mimir_decay`, `mimir_prune`, `mimir_purge`, `mimir_cohere`, `mimir_compact`, `mimir_reindex` |
| **Quality** | `mimir_score`, `mimir_conflicts`, `mimir_correct` |
| **Vault** | `mimir_vault_export`, `mimir_vault_import` |
| **Federation** | `mimir_federate`, `mimir_workspace_list` |
| **Metrics** | `mimir_stats`, `mimir_health`, `mimir_bench`, `mimir_synthesize` |

### Plugin Ecosystem

- [hermes-mimir-plugin](https://github.com/Perseus-Computing-LLC/hermes-mimir-plugin) — Native Perseus Vault integration for Hermes Agent
- [Perseus Perseus Vault Connector](https://github.com/Perseus-Computing-LLC/perseus) — Perseus live context injection from Perseus Vault

## Community Projects

*Add your project here! Open a PR to [awesome-mimir.md](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/awesome-mimir.md).*

## Articles & Tutorials

*Add articles, blog posts, and tutorials about Perseus Vault.*

## Comparisons

- [Perseus Vault vs Mem0](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/comparison/mimir-vs-mem0.md) — Local-first vs cloud-only
- [Perseus Vault vs Letta](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/comparison/mimir-vs-letta.md) — Memory engine vs agent runtime
- [Perseus Vault vs Zep](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/comparison/mimir-vs-zep.md) — Single binary vs infrastructure

## Key Differentiators

Why Perseus Vault stands out:

| Feature | Perseus Vault | Mem0 | Letta | Zep |
|---|---|---|---|---|
| **MCP-Native** | ✅ 55+ tools | ❌ | ❌ | ❌ |
| **Local-First** | ✅ Single binary | ❌ Cloud-dependent | ❌ Docker + Postgres | ❌ Docker + Postgres |
| **Zero Dependencies** | ✅ SQLite bundled | ❌ Python + vector DB | ❌ Python + Postgres | ❌ Go + Postgres |
| **Encryption at Rest** | ✅ AES-256-GCM | ❌ | ❌ | ❌ |
| **Hybrid Search** | ✅ FTS5 + Dense + RRF | Vector only | Vector only | Vector + Graph |
| **MIT License** | ✅ | Apache 2.0 | Apache 2.0 | Apache 2.0 |

## Contributing

See [CONTRIBUTING.md](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/CONTRIBUTING.md).

To add your project/resource to this list, open a PR against the `awesome-mimir.md` file.
