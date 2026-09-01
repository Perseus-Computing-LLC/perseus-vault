# Deprecated tool-prefix migration note

This note records the removal of pre-canonical MCP aliases. The current
`perseus_vault_*` namespace is the only advertised and dispatched tool surface.

## Current contract

- `tools/list` advertises exactly one copy of each tool under `perseus_vault_*`.
- `tools/call` accepts canonical `perseus_vault_*` names only; deprecated aliases
  are not dispatched.
- Deprecated alias environment variables are ignored; the alias machinery is gone.
- Deprecated default-database fallback locations are gone. The default store is
  always `~/.perseus-vault/data/perseus-vault.db` (or
  `$PERSEUS_VAULT_DB_PATH`).

## Migration

Clients that still call a pre-canonical name must switch to the canonical prefix.
The tool verb and argument schema are unchanged. Existing data from an older
installation must be copied or migrated explicitly into the canonical database
path before upgrading; it is never auto-adopted by guessing a legacy location.

| Current operation | Canonical MCP call |
|---|---|
| remember | `perseus_vault_remember` |
| recall | `perseus_vault_recall` |
| capture | `perseus_vault_capture` |
| health | `perseus_vault_health` |

For the complete current surface, see the generated registry and the
[Evaluator Guide](../EVALUATOR_GUIDE.md).