# Perseus Vault — MCP Client Setup

Perseus Vault is a standard **MCP stdio server**, so it works with every MCP-compatible
client. The command is always the same:

```
perseus-vault serve
```

Run `perseus-vault doctor` to validate your install and print this matrix locally.
Run `perseus-vault install-client` (alias of `connect`) to auto-wire a client's
config file — autodetects Claude Code / Codex / Cursor, or pass `--client <name>`
(`--all-detected` wires every detected client). It merges a `perseus-vault` MCP
stanza into the config (backing the original up as `<file>.bak-perseus` — no
manual JSON/YAML/TOML editing required), and with `--hooks --rules` it also
wires the full recall/capture loop: session lifecycle hooks plus the memory
usage-rules block per [docs/lifecycle-hooks.md](../lifecycle-hooks.md).
`--dry-run` previews every change; re-running is a no-op.
Run `perseus-vault prepare --task "<what you're about to do>"` for a pre-turn
memory-prep block — combines `recall_when` (proactive trigger matches
against the task text) and `context` (always-on + recent entities) into a
single `<memory-prep>...</memory-prep>` block, zero LLM calls, ~10-50ms.
Wire it into a Hermes/agent pre-turn hook so relevant memories are pushed
into context before the model sees the prompt, instead of depending on the
agent remembering to call `perseus_vault_recall_when` itself. `--json` emits
structured output for programmatic hooks.

Once your client is configured, see **[docs/lifecycle-hooks.md](../lifecycle-hooks.md)**
for the session lifecycle hook contract — copy-paste SessionStart/Stop hook
snippets for Claude Code, Codex, and Cursor that wire the recall → capture →
consolidate loop to session events, plus a portable AGENTS.md fallback.

## Working context versus durable memory

A client session has an active working context (the current prompt, transcript,
and any `prepare`/`perseus_vault_context` block) and a separate durable-memory
plane owned by the Vault server. Context is a bounded, rolling snapshot; refresh
it when the task changes, and do not assume that returning it persists the
host's prompt. Only an explicit `perseus_vault_remember`,
`perseus_vault_capture`, or equivalent write/capture result establishes
durability. Hooks are optional orchestration around the server-owned lifecycle,
not a second store.

If the server, a hook, or a refresh operation is unavailable, the client should
continue in degraded mode without injected memory, surface the failure, and
never claim that an unsuccessful write was saved or silently choose another
DB. See [retention, refresh, and erasure boundaries](../retention.md).

## Upgrade and migration

For a source-built upgrade, explicit database selection, encryption/doctor
checks, client-config dry runs and backups, restart, MCP smoke testing, and
rollback, follow the [upgrade and migration playbook](../migration/upgrade-playbook.md).
It deliberately does not assume a generic automatic database migration.

| Client | Status | Config file | Notes |
|---|---|---|---|
| Claude Desktop | ✅ Works | `claude_desktop_config.json` | Most common host |
| Claude Code / Hermes | ✅ Works | `.mcp.json` or `~/.hermes/config.yaml` | Verified |
| Cursor | ✅ Works | `.cursor/mcp.json` | |
| Windsurf | ✅ Works | `mcp_config.json` | |
| VS Code + Continue.dev | ✅ Works | `config.json` (`mcpServers`) | |
| Zed | ✅ Works | `settings.json` (`context_servers`) | |
| Codex CLI | ✅ Works | `~/.codex/config.toml` | |

---

## Copy-paste config

### Claude Desktop — `claude_desktop_config.json`
```json
{ "mcpServers": { "perseus-vault": { "command": "perseus-vault", "args": ["serve"] } } }
```

> **macOS extension users:** if the `.mcpb` extension shows "Could not connect
> to MCP server" immediately on macOS, the cause is the ad-hoc signed binary
> (#732). Use the stdio config above (unaffected) or see
> [docs/macos-signing.md](../macos-signing.md) for details and status.

### Claude Code — `.mcp.json` (project root)
```json
{ "mcpServers": { "perseus-vault": { "command": "perseus-vault", "args": ["serve"] } } }
```

### Hermes — `~/.hermes/config.yaml`
```yaml
mcp_servers:
  perseus-vault:
    command: perseus-vault
    args: ["serve"]
```

### Cursor — `.cursor/mcp.json`
```json
{ "mcpServers": { "perseus-vault": { "command": "perseus-vault", "args": ["serve"] } } }
```

### Windsurf — `mcp_config.json`
```json
{ "mcpServers": { "perseus-vault": { "command": "perseus-vault", "args": ["serve"] } } }
```

### VS Code + Continue.dev — `config.json`
```json
{ "mcpServers": { "perseus-vault": { "command": "perseus-vault", "args": ["serve"] } } }
```

### Zed — `settings.json`
```json
{ "context_servers": { "perseus-vault": { "command": { "path": "perseus-vault", "args": ["serve"] } } } }
```

### Codex CLI — `~/.codex/config.toml`
```toml
[mcp_servers.perseus-vault]
command = "perseus-vault"
args = ["serve"]
```

> `perseus-vault serve` defaults its database to `~/.perseus-vault/data/perseus-vault.db`
> (with a legacy fallback chain). Pass an absolute `--db` path if your client
> runs Perseus Vault from a different working directory or you want a specific
> location. Everything else is identical across clients because Perseus Vault
> speaks plain MCP stdio.
