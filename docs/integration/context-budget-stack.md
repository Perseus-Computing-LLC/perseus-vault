# Context Budget Stack — Perseus Vault · CoalWash · Noisegate

Three different tools own three different parts of an agent's context budget.
They are complementary, not competing, and they compose through the **existing**
[session lifecycle hooks](../lifecycle-hooks.md) — no tool has to know about the
others, and **nothing here changes what runs inside the Perseus Vault binary**.

| Layer | Tool | What it controls | When it acts |
|---|---|---|---|
| **Injected context** | **Perseus Vault** | *What memory enters the prompt.* Deterministic bi-temporal recall renders the right memories into the session. | SessionStart (recall) · on_insight (capture) |
| **Memory at rest** | [**CoalWash**](https://github.com/TheColliery/CoalWash) | *How lean the memory files are.* A memory washer/defragmenter that compacts the memory directory the agent reads from. | SessionStop / hygiene pass |
| **Runtime output** | [**Noisegate**](https://github.com/Tosko4/noisegate) | *How lean tool output is during a run.* Deterministic tool-output compaction plugin for Hermes Agent. | Mid-session, per tool call |

```
                 ┌─────────────────────── the model's context window ───────────────────────┐
   SessionStart  │  Perseus Vault recall  →  <memory-prep> block seeds the prompt            │
        work     │  tool calls  →  Noisegate compacts each tool's output before it lands      │
   on_insight    │  Perseus Vault capture  →  durable facts/decisions written to the vault    │
   SessionStop   │  Perseus Vault maintain (hygiene)  +  CoalWash defrag of memory files      │
                 └───────────────────────────────────────────────────────────────────────────┘
```

## Design boundary — why this is documentation, not a vault feature

Perseus Vault is a single, deterministic, air-gap-ready, security-audited binary
that **never spawns subprocesses**. Making the vault shell out to run
`coalwash` or to toggle a Hermes plugin would put third-party binary execution
inside the audited memory core — a supply-chain and determinism regression we
will not take. So the vault does not bundle, download, or execute either tool.

Instead you wire each tool where it already belongs: CoalWash in the same
**client** stop hook that already runs `perseus-vault maintain`, and Noisegate
in your **Hermes runtime** config. The vault stays a passive store; the client
and runtime own orchestration — exactly the split described in
[lifecycle-hooks.md](../lifecycle-hooks.md).

## Wiring CoalWash (memory at rest)

CoalWash operates on the *files* the vault and your instructions-file setup read
from (e.g. an exported `AGENTS.md` / memory directory), not on the encrypted
SQLite database. Run it in the same SessionStop hygiene step that already calls
`perseus-vault maintain`, and keep it in `--dry-run` until you trust the diff:

```bash
# SessionStop hook (client-side; runs in your shell, not in the vault)
perseus-vault maintain                       # vault-internal hygiene (reversible archives)
coalwash --dir "$MEMORY_DIR" --dry-run       # report what CoalWash would reclaim
# drop --dry-run once you've reviewed the output
```

Order matters: let the vault finish its own hygiene (cohere → decay → compact →
consolidate → dedup/orphans/reindex) first, then let CoalWash defragment
whatever memory files are rendered out. Both are reversible/preview-first by
design, so a dry run is safe to leave wired permanently.

## Wiring Noisegate (runtime output)

Noisegate is a Hermes Agent plugin that compacts tool output *as the agent
runs*, before it consumes budget. It is orthogonal to the vault: Perseus decides
what memory enters the prompt; Noisegate decides how much raw tool output is
allowed to. Enable it in your Hermes config alongside the Perseus MCP server:

```yaml
# Hermes runtime config (illustrative — see Noisegate's README for the plugin key)
plugins:
  - noisegate            # compacts tool output during the run
mcp_servers:
  perseus-vault:         # injects + captures memory (this repo)
    command: /usr/local/bin/perseus-vault
    args: ["--db", "~/.mimir/data/perseus-vault.db"]
```

With both active you get full budget control end to end: **Noisegate keeps
runtime output lean, Perseus keeps injected context relevant, CoalWash keeps
memory-at-rest lean.**

## Verifying the composition

- **Perseus:** `perseus-vault prepare --task "…"` prints the `<memory-prep>`
  block it would inject (local SQLite only, ~10–50 ms).
- **CoalWash:** run with `--dry-run` and inspect the reclaimed-bytes report
  before removing the flag.
- **Noisegate:** compare a tool call's raw vs. compacted output size in the
  Hermes run log.

None of these steps require network access, and none change the vault's
deterministic recall guarantees.

## See also

- [Session Lifecycle Hooks](../lifecycle-hooks.md) — the recall → work → capture
  → consolidate contract these tools plug into.
- [General MCP Integration](general-mcp.md) — wiring Perseus Vault into any MCP
  client.
