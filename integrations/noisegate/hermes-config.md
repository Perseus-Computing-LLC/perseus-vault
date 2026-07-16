# Hermes + Perseus Vault + Noisegate

Perseus Vault provides read-only **pre-turn context**; Noisegate compacts noisy
**runtime** tool/terminal output. They compose with **no runtime dependency**
between them.

## Install

- **Perseus Vault** — a single Rust binary (`perseus-vault`). Download a release
  or build from source and put it on `PATH`.
- **Noisegate** (Hermes plugin) — the PyPI package is **`noisegate-hermes`**
  (NOT `noisegate`, which is an unrelated project). Install/update with:
  ```sh
  uvx --from noisegate-hermes noisegate install-hermes
  ```

## MCP server config (Hermes)

```yaml
mcp_servers:
  perseus-vault:
    command: perseus-vault
    args: ["serve", "--db", "${HOME}/.perseus-vault/data/perseus-vault.db"]
```

Notes:
- Hermes interpolates `${HOME}` in MCP args but does **not** expand a literal `~`
  inside an argument — use `${HOME}` or an absolute path.
- `transport: stdio` is implied when `command`/`args` are present; omit it.

## Pre-turn context (optional)

Registering the MCP server exposes the tools — it does **not** by itself render
AGENTS.md or inject context. To push pre-turn memory into the prompt, run the
read-only `prepare` surface from a session-start hook/plugin that returns the
prepared context:

```sh
perseus-vault prepare --task "<current task>" --db "${HOME}/.perseus-vault/data/perseus-vault.db"
```

`prepare` is read-only (no writes) and designed to run every turn; see
[`fixtures.md`](./fixtures.md) for its output contract and
[`prepare_output.golden.json`](./prepare_output.golden.json) for a real sample.

## How it composes

1. Pre-turn: a hook runs `perseus-vault prepare` and injects the resulting
   `<memory-prep>` / context block into the system prompt.
2. Runtime: tool and terminal output flows through Noisegate's compaction.
   Noisegate leaves `mcp__*` tool results untouched, so Perseus Vault's MCP tool
   responses pass through unmodified with no Perseus-specific rule.

## Compatibility contract

The stable, regenerable output contract lives in [`fixtures.md`](./fixtures.md)
/ [`prepare_output.golden.json`](./prepare_output.golden.json). Note: Noisegate
does not currently ship Perseus-specific compatibility tests — don't claim it
does.
