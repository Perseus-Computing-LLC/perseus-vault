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

> `perseus-vault serve` defaults its database to `~/.mimir/data/perseus-vault.db`
> (with a legacy fallback chain). Pass an absolute `--db` path if your client
> runs Perseus Vault from a different working directory or you want a specific
> location. Everything else is identical across clients because Perseus Vault
> speaks plain MCP stdio.
