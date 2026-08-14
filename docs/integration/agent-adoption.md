# Agent Adoption: MCP-First

Perseus Vault is built for agents. This guide covers the adoption path where
the **agent is the consumer** — which is also the distribution lesson: when
the primary consumer is another agent, the interface is MCP, not a CLI
install. An agent adopts an MCP server without the human doing anything
beyond starting it once.

## Why MCP-first

- The Vault already speaks MCP over stdio (and SSE) — every MCP host connects
  with the same server command.
- No SDK, no per-machine install, no adapter files per editor. The agent's
  client negotiates `tools/list` and calls the canonical `perseus_vault_*`
  tools directly.
- The CLI is the *server-side* tool: run, verify, install clients, maintain.
  The *memory-side* surface is the tool registry.

## Connect (three steps)

```bash
# 1. Run the server
perseus-vault serve --db ~/.perseus-vault/data/perseus-vault.db &
```

```json
// 2. Register in the agent's MCP client config
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": ["serve", "--db", "~/.perseus-vault/data/perseus-vault.db"]
    }
  }
}
```

```bash
# 3. Verify from the agent's side
perseus-vault doctor
```

For one-command wiring of the full recall/capture loop (Claude Code, Codex,
Cursor, Hermes, Windsurf, Zed, vscode, claude-desktop, generic):

```bash
perseus-vault install-client --hooks --rules
```

Per-client guides: [claude-code.md](claude-code.md), [cursor.md](cursor.md),
[general-mcp.md](general-mcp.md).

## Tools by job

The full registry is grouped by category in the README. Agents should pick by
**job** instead:

| Job | Tools |
|---|---|
| Remember a durable fact / decision / correction | `perseus_vault_remember`, `perseus_vault_capture`, `perseus_vault_journal`, `perseus_vault_correct` |
| Recall before planning | `perseus_vault_recall`, `perseus_vault_recall_batch`, `perseus_vault_recall_when`, `perseus_vault_context`, `perseus_vault_ask` |
| Reconstruct the development narrative | `perseus_vault_handoff_pack` (`include_intent_trail`, `include_next_work`), `perseus_vault_delegation_brief`, `perseus_vault_timeline`, `perseus_vault_traverse` |
| Decisions: supersession and authority | `perseus_vault_supersede`, `perseus_vault_history`, `perseus_vault_authority_get`, `perseus_vault_action_receipt_get`, `perseus_vault_keystone_get` |
| Ask "what did we believe then?" | `perseus_vault_as_of`, `perseus_vault_valid_at`, `perseus_vault_bitemporal`, `perseus_vault_history` |
| Correct the record / surface contradictions | `perseus_vault_correct`, `perseus_vault_supersede`, `perseus_vault_conflicts`, `perseus_vault_reject_value` |
| Policy that survives compaction | `perseus_vault_keystone_get`, `perseus_vault_keystone_set` |
| Ops, trust, and scope | `perseus_vault_health`, `perseus_vault_stats`, `perseus_vault_agent`, `perseus_vault_workspace_status`, `perseus-vault doctor` (CLI) |

## The planning-boundary pattern

The highest-value use of the Vault is not per-invocation context stuffing; it
is **at the planning boundary**, when an agent is about to decide what work
means:

1. `perseus_vault_recall_when` — entities that declared they should fire for
   the current context surface themselves (proactive anticipation, #875
   telemetry).
2. `perseus_vault_handoff_pack` — a bounded, lifecycle-filtered context pack
   under a hard token budget: superseded/expired entities excluded with
   reasons, provenance-tagged, deterministic digest. With
   `include_intent_trail` and `include_next_work` it also carries the recent
   journal narrative (last plan / result / checkpoint) and forward plans.
3. `perseus_vault_delegation_brief` — the pack rendered as a self-contained
   markdown handoff (goal, scope, binding context, do-not-resurrect list,
   next work, output contract) that a parent agent hands to a subagent
   instead of the chat session.

The pack belongs at the planning boundary, not in every model invocation:
search the history, recover the relevant boundaries and prior intent, pack
that context into the plan, then execute against a relatively stable plan.

## Scope, identity, and authority

- Reads and writes are **workspace-scoped** when a `workspace_hash` is
  supplied; the handoff/delegation surfaces are in the scoped read set, so a
  bound profile cannot pull another workspace's decisions into a pack.
- The MCP transport stamps `requesting_agent_id` from the captured session
  and overwrites caller-supplied values — no model can claim another agent's
  identity.
- Mutation paths are fail-closed under the authority manifest (AAR control
  plane): `perseus_vault_action_intent` before external actions,
  `perseus_vault_authority_get` to inspect what an agent may do,
  `perseus_vault_action_receipt_get` for durable receipts.
- Best practice for multi-agent fleets: register agents
  (`perseus_vault_agent`) and bind profiles to workspaces
  (`perseus_vault_workspace_bind`) so the governance surface matches the
  topology.

## CLI vs MCP surface

| Need | Surface |
|---|---|
| Start / stop / verify the server | `perseus-vault serve`, `perseus-vault doctor` |
| Wire clients + lifecycle hooks | `perseus-vault install-client --hooks --rules` |
| Export / import vaults | `perseus-vault export`, `import` (or `perseus_vault_vault_export` / `_import`) |
| Everything an agent does with memory | MCP tools (`perseus_vault_*`) |
