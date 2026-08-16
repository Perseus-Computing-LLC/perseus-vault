# Integrating Perseus Vault with DeepSeek Harness

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) is
DeepSeek's open-source agent harness — a Cordis plugin runtime where everything,
including model adapters, tools, and the agent loop, is a plugin. Perseus Vault
connects two ways:

1. **MCP config row** — the vault's agent-safe tool surface as
   `mcp__perseus_vault__*` tools (48 tools; no admin/ops tools advertised).
2. **Native memory plugin** — push-based injection: relevant memories are
   placed into the system prompt before every user turn via
   `perseus-vault prepare`, so the agent doesn't have to remember to call
   recall itself.

Both live in the dedicated integration repo:
[`Perseus-Computing-LLC/perseus-vault-dsh`](https://github.com/Perseus-Computing-LLC/perseus-vault-dsh).

> **Developer preview.** `dsh` iterates rapidly with compatibility-breaking
> changes. The integration is tested against `dsh` `0.1.0-rc.6` and
> `perseus-vault` `2.23.0` (pins recorded in the integration repo).

## Quick Start (MCP tools)

```bash
# 1. Install Perseus Vault (pinned release)
cargo install perseus-vault@2.23.0
# or: curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash

# 2. Grab the DSH overlay from the integration repo and run dsh with it
npx @deepseek-ai/dsh web \
  --patch ./perseus-vault-dsh/examples/dsh-mcp/perseus-vault.cordis.yml
```

The agent now sees `mcp__perseus_vault__remember`, `recall`, `recall_when`,
`semantic_search`, `context`, `capture`, `journal`, and the rest of the
agent-tier surface. The overlay sets `PERSEUS_VAULT_TOOL_SCOPE=agent`, so
administrative tools (`purge`, `erase`, `migrate`, `authority_*`, …) are never
advertised to the model.

## Automatic memory injection (native plugin)

```yaml
# overlay.yml — mount alongside (or instead of) the MCP row
- insert:
    - id: perseus-vault-memory
      name: '@perseus-vault/dsh'
      config:
        command: perseus-vault
```

```sh
npx @deepseek-ai/dsh web --patch ./overlay.yml
```

On every step that carries a user message the plugin runs
`perseus-vault prepare` (local SQLite recall — no LLM calls, no network) and
injects the resulting `<memory-prep>` block as a system-prompt section.
Failure modes degrade silently: a missing binary or timeout renders an empty
section and never breaks the agent loop.

## Remote vaults

For a vault running as a service, use the `streamable-http` row
(`perseus-vault-http.cordis.yml` in the integration repo) with a bearer token
supplied via an ambient variable — never paste secrets into YAML.

## Verification

A controlled three-run matrix (control / MCP row / plugin-only) with a fact
that no model can know a priori is committed in the integration repo at
[`docs/VERIFICATION.md`](https://github.com/Perseus-Computing-LLC/perseus-vault-dsh/blob/main/docs/VERIFICATION.md).
