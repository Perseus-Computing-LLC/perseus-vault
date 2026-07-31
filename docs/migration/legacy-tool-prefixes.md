# Legacy Mimir and Mneme Tool Prefixes

Perseus Vault is the canonical product name. New MCP clients and integrations must use the canonical `perseus_vault_*` tool prefix.

## Compatibility commitment

The legacy `mimir_*` and `mneme_*` prefixes remain callable throughout the v2 release line. They are compatibility aliases for the equivalent `perseus_vault_*` tool and are not separate products or backends.

By default, `tools/list` advertises only canonical `perseus_vault_*` names. This keeps connected clients from loading three copies of every tool schema.

## Migration

Replace only the prefix; the tool verb and argument schema stay the same.

| Legacy call | Canonical call |
|---|---|
| `mimir_remember` | `perseus_vault_remember` |
| `mneme_remember` | `perseus_vault_remember` |
| `mimir_recall` | `perseus_vault_recall` |
| `mneme_recall` | `perseus_vault_recall` |
| `mimir_capture` | `perseus_vault_capture` |
| `mneme_capture` | `perseus_vault_capture` |
| `mimir_health` | `perseus_vault_health` |
| `mneme_health` | `perseus_vault_health` |

The same replacement applies to every supported tool verb.

## Older clients that need advertised aliases

A current client should call canonical names directly and leave the default advertisement mode unchanged.

An older client that requires legacy names to appear in `tools/list` can temporarily set:

```text
PERSEUS_VAULT_TOOL_ALIASES=all
```

The legacy `MIMIR_TOOL_ALIASES` environment variable is also honored. This is a bridge for old clients, not a recommended default.

## Migration evidence

`perseus_vault_alias_usage` provides process-local `canonical_calls`, `mimir_calls`, `mneme_calls`, and `other_calls` totals plus `since_process_start_unix_ms`. It contains no tool arguments, memory content, entity identifiers, credentials, client identity, or persisted analytics; it resets on server restart and does not count its own canonical or legacy-alias readouts.

The readout is for maintained-deployment migration reviews only. It is not centralized telemetry and must not be used to infer global adoption.

## Future removal

The current dated review is [Legacy Tool-Prefix Compatibility Review — 2026-07-25](../compatibility/legacy-prefix-review-2026-07.md) and its decision is **NO-GO**.

A removal of the legacy prefixes is v3-only and will not be scheduled until all of the following are met:

1. At least two stable v2 releases and 90 days of observed migration evidence.
2. Maintained clients use canonical calls without `PERSEUS_VAULT_TOOL_ALIASES=all`.
3. Clean-install and upgrade migration walkthroughs pass.
4. No unresolved compatibility reports remain for 90 days.
5. A dated compatibility report publishes the evidence, limitations, and an explicit go/no-go decision.

Database paths, persisted data, config migration, package names, and legacy redirects have independent compatibility policies; MCP prefix retirement does not remove them.
