# Session Lifecycle Hooks — the recall → work → capture → consolidate loop

Perseus Vault is a passive store until something wires it to your session's
lifecycle. Inside the Perseus stack that wiring is built in; in every other MCP
client (Claude Code, Codex, Cursor, …) the tools are all there but nothing
calls them automatically. This document defines the small, stable contract
that closes the loop in **any** client, plus copy-paste hook snippets for the
clients that support lifecycle hooks today.

Everything here is **optional and local**. No hook is required to use the
vault; each one just removes a "remember to call the tool" burden from the
agent. All commands run against your local SQLite database — no network, no
cloud, no telemetry.

```
SessionStart ──▶ recall (seed context)
     │
     ▼
   work ──▶ on_insight ──▶ remember (capture)
     │
     ▼
SessionStop ──▶ consolidate / compact (promote + hygiene)
```

## The contract

| Stage | Intent | MCP tools (canonical) | CLI (hook-friendly) |
|---|---|---|---|
| **SessionStart** | Proactive recall: seed the session with relevant memories; optionally scope to a workspace | `perseus_vault_context` (pass `query`), `perseus_vault_recall_when`, `perseus_vault_recall` | `perseus-vault prepare --task "<what this session is about>"` |
| **on_insight** (mid-session) | Capture a durable fact, decision, or lesson the moment it happens | `perseus_vault_remember` (facts/decisions), `perseus_vault_journal` (events), `perseus_vault_capture` (distill a raw payload — see [Automatic capture](#automatic-capture-520)) | `perseus-vault write --category <c> --key <k> --body '<json>'`, or `... \| perseus-vault capture` for raw payloads |
| **SessionStop** | Promote what was learned, merge duplicates, archive decayed noise | `perseus_vault_consolidate`, `perseus_vault_compact`, `perseus_vault_decay` | `perseus-vault maintain` (one verb: cohere → decay → compact → consolidate → dedup/orphans/reindex) |

Notes on the mapping:

- **SessionStart.** `perseus-vault prepare` is purpose-built for hooks: it runs
  `recall_when` (proactive trigger matching against the task text) plus the
  recall-first `context` block and prints a single `<memory-prep>…</memory-prep>`
  markdown block on stdout — local SQLite queries only, no LLM calls, typically
  10–50 ms. Print it into the session and the model starts with memory already
  in context. `--json` emits structured output for programmatic hooks.
  - *Workspace resolution (optional):* pass `--workspace <hash>` to `prepare`
    (or `workspace_hash` on the MCP tools) to scope injection to one project's
    memories. Single-workspace vaults can ignore this entirely.
- **on_insight.** No client fires an "insight happened" event — this stage is
  agent-initiated. The portable way to wire it is an instructions-file rule
  (see [the fallback below](#portable-fallback-agentsmd--instructions-file)).
  For raw material the agent shouldn't have to shape into `remember` calls
  itself (a transcript chunk, a pasted error + fix, a JSONL insight stream),
  #520 adds a distillation step — see
  [Automatic capture](#automatic-capture-520).
- **SessionStop.** `maintain` is the whole hygiene pass in one verb, designed
  for unattended runs: every effect is a reversible archive (never a hard
  delete), `--dry-run` previews, and `--vacuum` is opt-in (throttle to
  ~weekly). If your client's stop event fires per *turn* rather than per
  *session* (see the per-client notes), guard it or schedule `maintain`
  nightly instead — both patterns are shown below.
- **Naming caution:** `perseus_vault_ingest` is *connector sync* (GitHub
  issues, file watcher) — it is **not** "ingest the session transcript".
  Session capture is `remember`/`write`/`capture`; promotion of what was
  captured is `consolidate`.

## Automatic capture (#520)

The `on_insight` stage above assumes the agent shapes each insight into a
`remember` call. #520 removes that burden for raw payloads: the capture
pipeline distills a transcript / insight payload into durable entities
directly, so a solved problem persists the moment it happens.

- **CLI:** `perseus-vault capture` reads stdin (or `--file`) — plain text,
  markdown, or JSONL, auto-detected — and writes the distilled notes
  (root-cause / pitfall / decision / pattern / takeaway, `source="capture"`).
- **MCP:** `perseus_vault_capture` (same pipeline, same report shape).

**Still opt-in, still local-first.** Nothing captures automatically: no
config flag turns this on in the background, and the default distiller is
deterministic local Rust — no LLM, no network (`--llm` opts into the
configured endpoint and falls back on any failure). Capture runs only when
a hook or agent explicitly invokes it. Flood control is built in: trigram
near-duplicate merging stays ON (a re-captured insight merges instead of
piling up), and writes are hard-capped per invocation.

Wiring it into this contract:

- **on_insight:** pipe the insight in the moment it happens —
  `echo "<what was just learned>" | perseus-vault capture` — or have the
  agent call `perseus_vault_capture` with the raw material.
- **SessionEnd:** capture **then** `maintain` — persist what was learned,
  then groom it (the captured notes land in the `buffer` layer, so the
  hygiene pass immediately gets a chance to merge/promote them):

  ```json
  "SessionEnd": [
    { "matcher": "*",
      "hooks": [
        { "type": "command",
          "command": "cat \"$CLAUDE_TRANSCRIPT_PATH\" | perseus-vault capture",
          "timeout": 60 },
        { "type": "command",
          "command": "perseus-vault maintain",
          "timeout": 120 }
      ] }
  ]
  ```

  Preview with `--dry-run` first to see what your transcripts distill into.

Full reference (payload shapes, classification, the report fields, the
optional LLM path): [docs/capture.md](capture.md).

## Tool-name stability promise

Hooks and instructions files hard-code tool names, so those names are a
contract:

- Every tool is exposed under **three interchangeable prefixes**:
  `perseus_vault_*` (canonical), plus `mimir_*` and `mneme_*` (legacy aliases
  from earlier product names). The server advertises all three in
  `tools/list`, and `tools/call` normalizes any of the three prefixes to the
  same handler (see `src/mcp.rs`).
- **Write new hooks against `perseus_vault_*`.** Existing hooks written
  against `mimir_*` or `mneme_*` keep working: all three prefixes are
  supported for the lifetime of the v2 series and will not be removed in a
  minor or patch release. New tools always ship under all three prefixes.
- CLI verbs referenced by this contract (`prepare`, `write`, `capture`,
  `maintain`, `stats`) follow the same policy: stable for the v2 series; any
  future rename would keep the old verb as an alias through a deprecation
  window.

## Claude Code

*Verified against the official hooks reference at
[code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks).*

Claude Code hooks live in `.claude/settings.json` (project) or
`~/.claude/settings.json` (user). Two event facts matter here:

- `SessionStart` supports matchers (`startup`, `resume`, `clear`, `compact`),
  and **anything the hook prints to stdout is added to Claude's context** —
  which is exactly what `prepare` emits.
- `Stop` fires at the end of **every turn**, not the session. Use
  `SessionEnd` for the end-of-session hygiene pass.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "perseus-vault prepare --task \"$(basename \"$PWD\")\"",
            "timeout": 30,
            "statusMessage": "Recalling from Perseus Vault..."
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "perseus-vault maintain",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

The `SessionStart` command is shell-form, so `$(basename "$PWD")` resolves to
the project directory name and becomes the recall task — swap in any richer
task description you like (e.g. read `source` from the stdin JSON with `jq`).
If `perseus-vault` is not on the `PATH` Claude Code launches with, use the
absolute binary path. Pass `--db /abs/path/to/perseus-vault.db` on both
commands if you don't use the default database location.

Mid-session capture in Claude Code is agent-initiated — add the
[fallback rules block](#portable-fallback-agentsmd--instructions-file) to your
`CLAUDE.md` so the agent calls `perseus_vault_remember` when it learns
something durable.

## Codex (OpenAI Codex CLI)

*Verified against the official hooks reference at
[developers.openai.com/codex/hooks](https://developers.openai.com/codex/hooks).*

Codex loads hooks from `~/.codex/hooks.json` / `<repo>/.codex/hooks.json` (or
inline `[hooks]` tables in `config.toml`) using the same event schema as
Claude Code: `SessionStart`, `Stop`, `UserPromptSubmit`, and friends. A
`SessionStart` hook's plain stdout is treated as additional context (or emit
`hookSpecificOutput.additionalContext` JSON for the explicit form).

Codex has no `SessionEnd` event — `Stop` fires when the agent loop ends,
i.e. per turn. The snippet below therefore guards the hygiene pass with a
once-per-day stamp file so `maintain` doesn't run after every response:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "perseus-vault prepare --task \"$(basename \"$PWD\")\"",
            "statusMessage": "Recalling from Perseus Vault..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'STAMP=\"$HOME/.perseus-vault/.maintain-$(date +%F)\"; [ -f \"$STAMP\" ] || { perseus-vault maintain && mkdir -p \"$HOME/.perseus-vault\" && touch \"$STAMP\"; }'",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

Equivalent `config.toml` form:

```toml
[[hooks.SessionStart]]
matcher = "startup|resume"

[[hooks.SessionStart.hooks]]
type = "command"
command = "perseus-vault prepare --task \"$(basename \"$PWD\")\""
statusMessage = "Recalling from Perseus Vault..."

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = "sh -c 'STAMP=\"$HOME/.perseus-vault/.maintain-$(date +%F)\"; [ -f \"$STAMP\" ] || { perseus-vault maintain && mkdir -p \"$HOME/.perseus-vault\" && touch \"$STAMP\"; }'"
timeout = 120
```

Codex requires you to **trust** non-managed hooks before they execute — run
`/hooks` in Codex to review and approve them (trust is recorded against the
hook's hash, so editing the command requires re-approval).

## Cursor

*Config format verified against
[cursor.com/docs/hooks](https://cursor.com/docs/hooks); Cursor's hook surface
is newer and still evolving, so treat behavior details as best-effort and
re-check the docs if a snippet misbehaves.*

Cursor hooks live in `.cursor/hooks.json` (project) or `~/.cursor/hooks.json`
(user). The relevant events are `sessionStart` (fires when a new agent
conversation is created; its JSON output can inject `additional_context`) and
`stop` (fires when the agent loop ends). Unlike Claude Code/Codex,
`sessionStart` context injection requires **JSON output**, not plain stdout,
so wrap `prepare` in a tiny script:

`.cursor/hooks/perseus-vault-recall.sh`

```bash
#!/usr/bin/env bash
# Read hook input (unused here, but consume stdin), emit additional_context.
cat > /dev/null
CTX="$(perseus-vault prepare --task "$(basename "$PWD")" 2>/dev/null)"
jq -n --arg ctx "$CTX" '{ "additional_context": $ctx }'
```

`.cursor/hooks.json`

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [
      { "command": "./.cursor/hooks/perseus-vault-recall.sh" }
    ],
    "stop": [
      { "command": "sh -c 'STAMP=\"$HOME/.perseus-vault/.maintain-$(date +%F)\"; [ -f \"$STAMP\" ] || { perseus-vault maintain && mkdir -p \"$HOME/.perseus-vault\" && touch \"$STAMP\"; }'" }
    ]
  }
}
```

Make the script executable (`chmod +x .cursor/hooks/perseus-vault-recall.sh`).
The `stop` hook reuses the once-per-day guard because Cursor's `stop` fires
per agent loop, not per editor session.

### Rovo Dev CLI — use AGENTS.md, not a session-start hook

**Rovo Dev's event hooks cannot inject startup context (yet), so do not wire
memory-prep through a hook.** As of this writing Rovo Dev CLI hooks fire only on
side-effect events — `on_tool_permission`, `on_complete`, `on_error` — there is
**no session-start event**, and **hook stdout is not fed back into the
assistant's context** (Atlassian describes hook input/output support as still
being *explored*, not shipped —
[event-hooks blog](https://www.atlassian.com/blog/development/streamline-rovo-dev-cli-with-event-hooks)).
A `~/.rovo/hooks/.../session_start.sh` that prints a `<memory-prep>` block will
run and produce correct output when invoked directly, but a fresh Rovo session
never sees it — the hook output goes nowhere.

What Rovo *does* load at startup is **`AGENTS.md`**. So for Rovo, route recall
through the file, not a hook: render the memory-prep into `AGENTS.md` before the
session (the same "render a file, refresh before start" pattern as Hermes'
`.hermes.md` above), e.g.

```bash
# refresh AGENTS.md before a Rovo session (cron / watch / manual)
perseus-vault prepare --task "$(basename "$PWD")" > .perseus-vault-recall.md
# then include/concatenate that block into the AGENTS.md Rovo reads
```

Keep it fresh with a scheduler or file watch, exactly like the other
render-a-file profiles. (The always-on + `recall_when` block is a snapshot at
render time — re-render when the task changes.) Track turnkey `AGENTS.md`
integration in perseus#790.

## Portable fallback: AGENTS.md / instructions file

If your client has no lifecycle hooks (or you'd rather not configure them),
the loop still works as a *convention*: put a usage-rules block in whatever
instructions file your client reads — `AGENTS.md`, `CLAUDE.md`, `.cursorrules`,
a Zed/Windsurf rules file, or a system prompt. This is passive (it depends on
the model following instructions rather than the client enforcing them), but
it is fully portable:

```markdown
## Memory (Perseus Vault)

You have persistent memory via the perseus_vault_* MCP tools. Follow this loop:

1. **Session start:** before your first substantive action, call
   `perseus_vault_context` with `query` set to the current task (or
   `perseus_vault_recall` with topic keywords) and treat the results as
   established context.
2. **During work:** whenever a durable fact, decision, constraint, or lesson
   is established, immediately call `perseus_vault_remember` with a clear
   `category`, a stable `key`, and the fact in `content`. Set `recall_when`
   triggers describing when it should resurface. Record significant events
   with `perseus_vault_journal`.
3. **Before finishing:** if this session produced several related memories,
   call `perseus_vault_consolidate` (with `dry_run: true` first) to merge
   overlap into durable observations.

Do not store secrets, credentials, or transient scratch state as memories.
```

Even with hooks configured, keeping rule 2 in your instructions file is
recommended — hooks cover start/stop; mid-session capture is still
agent-initiated, though [#520](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/520)'s
`capture` verb/tool ([Automatic capture](#automatic-capture-520)) means the
agent only has to hand over the raw material, not shape each memory itself.

## Scheduled upkeep instead of stop hooks

Session-stop hygiene is a convenience, not a requirement. If you prefer (or
your client has no stop event), run the same pass on a schedule:

```bash
# cron (nightly hygiene, weekly vacuum)
15 3 * * *  perseus-vault maintain --db /abs/path/perseus-vault.db
30 3 * * 0  perseus-vault maintain --db /abs/path/perseus-vault.db --vacuum
```

Windows: use Task Scheduler (`schtasks`) with the same commands. Long-lived
servers can pass `--maintain-every <hours>` to `perseus-vault serve` as a
no-cron fallback.

## Verify the loop

Prove memory survives a session boundary. With the vault configured in your
client and (optionally) the hooks above installed:

1. **Session A — capture.** Tell the agent:
   > Remember this decision: we chose SQLite WAL mode for the cache layer
   > because Redis added an operational dependency.

   The agent should call `perseus_vault_remember`. Confirm from a terminal:

   ```bash
   perseus-vault stats            # entity count includes the new memory
   perseus-vault prepare --task "cache layer decision"
   # → the <memory-prep> block should contain the SQLite WAL decision
   ```

2. **End session A.** Close the session. If a stop hook is installed, it runs
   `perseus-vault maintain`; check with `perseus-vault stats` (or run
   `perseus-vault maintain --dry-run` to see what a pass would do).

3. **Session B — recall.** Start a fresh session (new conversation, no shared
   context) and ask, *without restating the fact*:
   > What did we decide about the cache layer, and why?

   - With a `SessionStart` hook: the `<memory-prep>` block was injected before
     your prompt, so the agent answers from seeded context.
   - Without hooks (instructions-file fallback): the agent should call
     `perseus_vault_recall` or `perseus_vault_context` first, then answer.

   Either way, the answer is "SQLite WAL mode, because Redis added an
   operational dependency" — recalled, not guessed.

## Related work

- [#520 — Automatic in-session memory capture via lifecycle hooks](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/520):
  shipped as the `capture` verb + `perseus_vault_capture` tool — see
  [Automatic capture](#automatic-capture-520) above and
  [docs/capture.md](capture.md).
- [#521 — Failure-pattern / déjà-vu guard](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/521):
  a pre-retry check that will slot naturally into `PreToolUse`-style hooks.
- [docs/clients/README.md](clients/README.md) — MCP server config snippets for
  every client (the prerequisite for everything above).
- [docs/integration/claude-code.md](integration/claude-code.md) and
  [docs/integration/cursor.md](integration/cursor.md) — full client setup guides.
