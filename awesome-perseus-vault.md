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
- [perseus_vault-langgraph](https://github.com/Perseus-Computing-LLC/perseus-vault/tree/main/integrations/langgraph) — `PerseusVaultStore` implementing `BaseStore`
- Drop-in persistent memory for LangGraph agents
- `pip install -e integrations/langgraph/`

### CrewAI
- [perseus_vault-crewai](https://github.com/Perseus-Computing-LLC/perseus-vault/tree/main/integrations/crewai) — `PerseusVaultMemoryTool` as a CrewAI agent tool
- Agents can remember, recall, journal, and get context
- `pip install -e integrations/crewai/`

### AutoGen (AG2 / autogen-core)
- [perseus_vault-autogen](https://github.com/Perseus-Computing-LLC/perseus-vault/tree/main/integrations/autogen) — `PerseusVaultMemory` implementing `autogen_core.memory.Memory`
- Context injection before each inference turn
- `pip install -e integrations/autogen/`

### Other Frameworks
Perseus Vault is MCP-native — any framework with MCP support can use Perseus Vault directly:
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) — via MCP stdio
- [Google ADK](https://github.com/google/adk-python) — via MCP stdio
- [Agno](https://github.com/agno-agi/agno) — via MCP stdio
- [Magentic-One](https://github.com/anthropics/anthropic-quickstarts) — via MCP stdio
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) — MCP config + native memory-injection plugin ([perseus-vault-dsh](https://github.com/Perseus-Computing-LLC/perseus-vault-dsh))

## MCP Hosts

Perseus Vault works with any MCP host. Configuration is one line:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": ["serve", "--db", "~/.perseus-vault/data/perseus-vault.db"]
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

### Perseus Vault Itself (current registry; 169 canonical MCP tools)

| Category | Tools |
|---|---|
| **Entity CRUD** | `perseus_vault_remember`, `perseus_vault_recall`, `perseus_vault_recall_when`, `perseus_vault_get_entity`, `perseus_vault_capture`, `perseus_vault_forget` |
| **Graph** | `perseus_vault_link`, `perseus_vault_unlink`, `perseus_vault_traverse` |
| **Journal** | `perseus_vault_journal`, `perseus_vault_check_failure_pattern`, `perseus_vault_timeline` |
| **State** | `perseus_vault_state_set`, `perseus_vault_state_get`, `perseus_vault_state_delete`, `perseus_vault_state_list` |
| **Search & RAG** | `perseus_vault_ask`, `perseus_vault_embed`, `perseus_vault_context`, `perseus_vault_ingest` |
| **Lifecycle** | `perseus_vault_decay`, `perseus_vault_prune`, `perseus_vault_purge`, `perseus_vault_cohere`, `perseus_vault_compact`, `perseus_vault_reindex` |
| **Quality** | `perseus_vault_score`, `perseus_vault_conflicts`, `perseus_vault_correct` |
| **Vault** | `perseus_vault_vault_export`, `perseus_vault_vault_import` |
| **Workspace transfer** | `perseus_vault_workspace_list` (peer federation is intentionally disabled; use explicit export/import) |
| **Metrics** | `perseus_vault_stats`, `perseus_vault_health`, `perseus_vault_bench`, `perseus_vault_synthesize` |

### Plugin Ecosystem

- [hermes-perseus-vault-plugin](https://github.com/Perseus-Computing-LLC/hermes-perseus-vault-plugin) — Native Perseus Vault integration for Hermes Agent
- [Perseus Perseus Vault Connector](https://github.com/Perseus-Computing-LLC/perseus) — Perseus live context injection from Perseus Vault
- [perseus-vault-dsh](https://github.com/Perseus-Computing-LLC/perseus-vault-dsh) — DeepSeek Harness plugin: pre-step memory injection + MCP config example

## Community Projects

*Add your project here! Open a PR to [awesome-perseus-vault.md](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/awesome-perseus-vault.md).*

## Articles & Tutorials

*Add articles, blog posts, and tutorials about Perseus Vault.*

## Comparisons

- [Perseus Vault vs Mem0](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/comparison/perseus-vault-vs-mem0.md) — Local-first vs cloud-only
- [Perseus Vault vs Letta](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/comparison/perseus-vault-vs-letta.md) — Memory engine vs agent runtime
- [Perseus Vault vs Zep](https://github.com/Perseus-Computing-LLC/perseus-vault/blob/main/docs/comparison/perseus-vault-vs-zep.md) — Single binary vs infrastructure

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

To add your project/resource to this list, open a PR against the `awesome-perseus-vault.md` file.
