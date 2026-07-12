# Perseus Vault vs Letta: Memory Engine vs Agent Runtime

## Quick Summary

| | Perseus Vault | Letta |
|---|---|---|
| **Stars** | ~20 | ~15K |
| **What it is** | Pure memory engine | Full agent runtime with memory |
| **Language** | Rust | Python |
| **Deployment** | Single binary (~8MB) | Docker + PostgreSQL |
| **Dependencies** | Zero (SQLite bundled) | PostgreSQL, Python runtime |
| **MCP Tools** | 58 | 8 |
| **Memory Model** | Structured entities + journal + state | Agent state with blocks |
| **Composes with** | Any agent framework | Letta agents only |
| **Offline** | ✅ Fully local | ❌ Requires PostgreSQL |
| **Encryption** | AES-256-GCM | ❌ |
| **License** | MIT | Apache 2.0 |

## Measured: same-box recall (fully local)

Both run on one H100 against the same local Ollama (`qwen2.5:14b-instruct` +
`nomic-embed-text`), identical fact set / queries / substring judge:

| System | Recall | p50 |
|---|---|---|
| **Perseus Vault** (hybrid, in-process) | **1.00** | 35.6 ms |
| Letta (archival memory / pgvector) | 1.00 | 135.5 ms |

Letta was run live as the `letta/letta` server (bundled Postgres/pgvector) pointed at
the same local Ollama; the identical facts were seeded into archival memory and queried
via pgvector nearest-neighbor search. Both reach 1.00 recall on this corpus — the
difference is operational: Perseus Vault answers in-process from a single binary at
~35 ms, while Letta round-trips through a server + Postgres at ~135 ms p50. Full
artifact: [`benchmark/lambda/results/competitors.json`](../../benchmark/lambda/results/competitors.json).

## Architecture: Composable vs Monolithic

Perseus Vault is a **pure memory engine** — it does one thing (persistent memory) and
composes with any agent framework via MCP stdio. Letta is an **agent runtime**
that includes memory as one component.

### Perseus Vault: Memory as a Service

```
CrewAI ──────────┐
LangGraph ───────┤
AutoGen ─────────┼──MCP stdio── mimir (Rust binary)
Claude Desktop ──┤                    └── SQLite
Cursor ──────────┘
```

Perseus Vault plugs into any MCP host. Your agents can use CrewAI, LangGraph, AutoGen,
or any framework — Perseus Vault is just the memory layer.

### Letta: Everything in One Box

```
Letta Agent ── Letta Runtime ── PostgreSQL
                    ├── Memory blocks
                    ├── Tool execution
                    ├── Message history
                    └── Agent state
```

Letta agents run inside Letta's runtime. The memory is coupled to the agent
framework. You can't use Letta's memory with a CrewAI agent or a Claude Desktop
session.

## When to Use Perseus Vault

- You want memory that **works with any agent framework**
- You're building agents with CrewAI, LangGraph, AutoGen, or raw MCP
- You want a **single binary** with no database to manage
- You need **encryption at rest**
- You want **58 MCP tools** for the full memory lifecycle
- You need fully offline/air-gapped operation

## When to Use Letta

- You want a **complete agent platform** with built-in memory
- You're building Letta-native agents and want tight integration
- You need **advanced agent features** beyond memory (multi-agent orchestration, sandboxed execution)
- Your team is comfortable with Docker + PostgreSQL
- You want Letta's advanced memory management (archival memory, recall memory, core memory blocks)

## Memory Models

### Perseus Vault: Lifecycle-Aware Entities

Perseus Vault entities have a full lifecycle:
- **Ebbinghaus decay** — memories naturally fade unless retrieved
- **Layer promotion** — buffer → working → core based on access patterns
- **Automatic archival** — stale entities archive, recoverable
- **Conflict detection** — trigram similarity finds near-duplicates
- **Entity graph** — link entities with typed relationships
- **Journal** — append-only audit trail

### Letta: Memory Blocks

Letta organizes memory into blocks:
- **Core memory** (always loaded)
- **Recall memory** (conversation history)
- **Archival memory** (long-term storage)

This is effective for Letta's agent architecture but is **tied to Letta's
agent loop**. The memory blocks are managed by the Letta runtime, not by
a standalone memory service.

## MCP Tools: 58 vs 8

Perseus Vault's 58 MCP tools cover the entire memory surface. Letta exposes ~8 tools
focused on agent state management. Perseus Vault's additional tools enable:

- **mimir_correct** — structured learning from errors
- **mimir_synthesize** — LLM session synthesis
- **mimir_bench** — performance tracking
- **mimir_federate** — cross-workspace entity sharing
- **mimir_vault_export/import** — portable markdown format
- **mimir_purge** — permanent deletion with VACUUM reclaim

## Honest Assessment

**Perseus Vault's strengths vs Letta:**
- Composable with any framework (not locked into one agent runtime)
- Single binary, no PostgreSQL dependency
- 58 MCP tools vs 8
- Encryption at rest
- Full entity lifecycle management
- MIT license

**Perseus Vault's weaknesses vs Letta:**
- Letta has a more mature agent platform with advanced features
- Letta's memory block model is well-tested in production
- Letta provides managed cloud hosting (Letta Cloud)
- Larger community and more documentation
- Sandboxed code execution is Letta-native

**If you're choosing today:** Perseus Vault is the better choice if you want memory
that works with any agent framework. Letta is better if you want a complete
managed agent platform and are willing to commit to Letta's ecosystem.
