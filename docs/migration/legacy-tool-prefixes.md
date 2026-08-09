# Legacy Tool Prefixes — Removed

The `mimir_*` and `mneme_*` MCP tool prefixes, the `PERSEUS_VAULT_TOOL_ALIASES`
advertisement mode, and the `perseus_vault_alias_usage` readout tool were
**removed** in this release. The `perseus_vault_*` prefix is the only tool
prefix.

## What changed

- `tools/list` advertises exactly one copy of each tool under `perseus_vault_*`.
- `tools/call` accepts `perseus_vault_*` names only; legacy prefixes are not
  dispatched.
- The `PERSEUS_VAULT_TOOL_ALIASES` / `MIMIR_TOOL_ALIASES` environment variables
  are ignored (the alias machinery is gone).
- The legacy default-database fallback chain (`~/.mimir/`, `mneme.db`,
  `mimir.db`, `~/mimir.db`) is gone: the default store is always
  `~/.perseus-vault/data/perseus-vault.db` (or `$PERSEUS_VAULT_DB_PATH`).

## Migration

Clients that still call legacy names must switch to the canonical prefix. The
tool verb and argument schema are unchanged:

| Legacy call (removed)     | Canonical call               |
|---------------------------|------------------------------|
| `mimir_remember`          | `perseus_vault_remember`     |
| `mimir_recall`            | `perseus_vault_recall`       |
| `mimir_capture`           | `perseus_vault_capture`      |
| `mimir_health`            | `perseus_vault_health`       |

Existing on-disk data under the legacy `~/.mimir/` locations is no longer
auto-adopted; move it to `~/.perseus-vault/data/perseus-vault.db` explicitly
before upgrading.
