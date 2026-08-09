# Live-update / reconnect workflow (#858)

Status: implemented (branch `feat/vault-858-live-update`)
Surface: `perseus_vault_handoff_restart` + binary-staleness gate, `perseus_vault_health` fields

## Problem

A stdio MCP client (e.g. Rovo Dev on macOS) spawns the `perseus-vault` child
once per session. Rebuilding/replacing the binary mid-session left the running
process stale — and the failure mode was silent: Vault-backed calls returned
empty results until the whole session was restarted.

## Design

Three server-side mechanisms (client-agnostic; nothing to install on the
client):

### 1. Staleness detection

`live_update::BinaryIdentity` captures the running binary's identity once at
startup: path + `(dev, ino)` + `(len, mtime_ns)`.

- `dev/ino` identify the file *object* — a rename-replace (normal
  `cargo build`/`install` flow) changes the inode.
- `len/mtime_ns` catch in-place rewrites that reuse the same inode.
- On every tool call the current on-disk file is re-`stat`ed (one syscall)
  and compared. Deleted binary ⇒ stale.

### 2. Fail loud (never silently stale)

When the binary was replaced, **every tool except `perseus_vault_handoff_restart` and
`perseus_vault_health`** returns an explicit `isError` result:

```
perseus-vault: the running binary was replaced on disk (pid N, /path/to/binary);
refusing to serve results from a stale process image. Run
perseus_vault_handoff_restart with {"confirm": true} to hot-swap this session on
the same stdio connection, or restart the client session. To override for
diagnostics: PERSEUS_VAULT_IGNORE_STALE_BINARY=1
```

`perseus_vault_health` stays callable and reports `binary_stale`, `binary_path`, and
`pid` so a client can self-diagnose.

### 3. Hot-swap handoff (`perseus_vault_handoff_restart`)

Four states, all with clear feedback:

| state | trigger | behavior |
|---|---|---|
| `no_handoff_needed` | binary unchanged | identity report; no exec |
| `dry_run` | stale + `dry_run:true` | what would happen; no exec |
| `confirm_required` | stale, no `confirm:true` | clear feedback; no exec |
| `handoff_performed` | stale + `confirm:true` | spawn + exit (below) |

Handoff mechanics (all client-agnostic stdio):

1. The handler reports `handoff_performed` and sets a pending flag.
2. The MCP server loop **flushes the response first**, then spawns the
   replacement binary (`std::env::current_exe()` resolves to the new file)
   with the same argv and **inherited stdin/stdout/stderr** — the client's
   pipes never close, so the session continues in the new process image.
3. The old process exits; SQLite WAL recovers any unfinished state on the
   child's fresh open (same as the orphan-watcher exit path, #547).
4. The child is tagged `PERSEUS_VAULT_HANDOFF_CHILD=1`, which disables the
   orphan guards (Linux `PR_SET_PDEATHSIG`, ppid watcher thread, per-request
   ppid poll) — the handoff protocol is the liveness proof; stdin EOF remains
   the real client-death signal.

## Supported local workflow (macOS / Rovo Dev)

```bash
# 1. rebuild
cargo build --release

# 2. (optional) dry-run report: binary_stale=true, would_handoff=true
#    via any MCP client: perseus_vault_handoff_restart {"dry_run": true}

# 3. hot-swap the live session — the next response comes from the new binary
perseus_vault_handoff_restart {"confirm": true}
```

Constraints:

- **Do not pipeline requests during handoff** — the old process's stdin
  `BufReader` may hold read-ahead bytes that die with the process. MCP clients
  are strictly request/response, so this is a documented constraint, not a
  live hazard.
- If the session goes quiet after `handoff_performed`, the spawn failed (e.g.
  the binary was deleted rather than replaced); restart the client session.
- Diagnostics override: `PERSEUS_VAULT_IGNORE_STALE_BINARY=1` bypasses the
  fail-loud gate (stale results are still served).

## Verification

- `live_update` unit tests: in-place rewrite, rename-replace, deletion, gate
  matrix, all four report states, pending-flag roundtrip, untouched-binary
  handler path (8 tests).
- Full suite: 643 passed / 0 failed.
- The exec path is deliberately not exercised in-process (it would exec the
  test harness); it is covered by the report-state tests + code review.
