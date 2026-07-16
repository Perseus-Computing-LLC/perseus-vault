# Hermes + Perseus Vault + Noisegate — Full Context Budget Stack

Copy-paste into your Hermes `config.yaml` to wire all three together.
Perseus handles context injection (AGENTS.md rendering). Noisegate handles
runtime tool-output compaction (MCP tool results are preserved verbatim by default).

## 1. MCP server config

```yaml
mcp_servers:
  perseus-vault:
    command: perseus-vault
    args: ["serve", "--db", "~/.perseus-vault/data/perseus-vault.db"]
    transport: stdio
```

## 2. Noisegate plugin

```bash
pip install noisegate
noisegate init
```

Noisegate hooks into `transform_tool_result` and `transform_terminal_output`.
It already preserves MCP/memory-tool results verbatim. Perseus Vault tools are
MCP tools — they get preservation automatically. No additional config needed.

The compatibility contract is defined by the fixtures at `integrations/noisegate/fixtures.md`.
Noisegate's byte-exact compatibility tests verify that Perseus Vault's MCP tool
responses pass through unmodified.

## 3. Session start hook (optional)

```yaml
hooks:
  pre_llm_call:
    - command: "perseus-vault write --category system --key session-start --body '{\"agent\":\"hermes\"}' --tags session-start"
      timeout: 5
```

## What this does

1. Agent starts → Perseus Vault renders AGENTS.md context via `perseus_vault_context`
2. Agent works → tools produce output that flows through Noisegate's `transform_terminal_output` hook
3. Noisegate compacts noisy tool output → MCP tool results (including Perseus Vault) preserved verbatim
4. Agent stays focused on signal, both input and output sides

Both projects remain independently usable. No runtime dependency between them.
