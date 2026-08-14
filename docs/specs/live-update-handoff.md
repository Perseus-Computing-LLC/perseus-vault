# Live-update / reconnect workflow (#858, #1045)

Status: implemented (#858), repaired and completed (#1045)
Surface: `perseus_vault_handoff_restart` + binary-staleness gate, `perseus_vault_health` fields,
`PERSEUS_VAULT_AUTO_HANDOFF`, session-state forwarding, window-free handoff

## Problem

A stdio MCP client (e.g. Rovo Dev on macOS) spawns the `perseus-vault` child
once per session. Rebuilding/replacing the binary mid-session left the running
process stale — and the failure mode was subtle: calls degraded into empty
results until the whole session was restarted.

#858 shipped detection + a fail-loud gate + a handoff tool, but the handoff
path had three defects that #1045 exposed:

1. **The replacement process never resumed the session.** The new image is a
   fresh process whose `initialized` flag is false, and the client (which
   already initialized the old image) never re-sends `initialize`. Every
   post-handoff `tools/call` answered `-32002 "Not initialized"`, and the
   transport-captured agent identity (#684/#855) was lost — visibility-scoped
   recall degraded to empty results. This was invisible because the exec path
   was never exercised end-to-end (only report-state unit tests).
2. **Linux: `current_exe()` returns the path with a `" (deleted)"` suffix**
   after the binary's file is replaced via rename(2). Stat-ing/exec-ing that
   literal path fails with ENOENT, so staleness detection degraded and the
   handoff could never succeed on Linux at all.
3. **The flush-then-exec window dropped requests.** The pre-fix sequence
   (write the `handoff_performed` report, then exec) left a gap in which a
   client's next request could be consumed by the dying image's stdin
   `BufReader` and vanish — the session looked dead even though the swap
   succeeded.

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
- The executable path is normalized via `executable_path()`: the Linux
  `/proc/self/exe` `" (deleted)"` suffix is stripped so both detection and
  the handoff target the real on-disk file (#1045).

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
| `no_handoff_needed` | binary unchanged | identity report; no swap |
| `dry_run` | stale + `dry_run:true` | what would happen; no swap |
| `confirm_required` | stale, no `confirm:true` | clear feedback; no swap |
| `handoff_performed` | stale + `confirm:true` | window-free swap (below) |

Handoff mechanics (#1045 window-free contract):

1. The handler prepares the `handoff_performed` report and schedules the
   swap — **the stale image does not write the report itself**. The
   serialized response is stashed (`PERSEUS_VAULT_HANDOFF_PENDING_RESPONSE`)
   and the loop hands off *instead of* writing, so there is no window
   between a response flush and the image swap for a client request to fall
   into.
2. The swap: **Unix** — `exec` replaces the process image in place (same
   PID, same stdio pipes, orphan guards keep watching the unchanged parent).
   **Windows** — `exec` does not exist, so the replacement is spawned with
   inherited stdio and tagged `PERSEUS_VAULT_HANDOFF_CHILD=1` (orphan
   watchers disabled; stdin EOF remains the client-death signal).
3. **Session state is forwarded** (`PERSEUS_VAULT_HANDOFF_STATE` =
   `{initialized, session_agent_id}`). The replacement image restores it at
   startup: it considers itself initialized (no second handshake exists) and
   re-installs the transport-captured agent identity, re-sanitized exactly
   like the `initialize` path. The env vars are cleared after reading so no
   further self-spawned child inherits them.
4. The replacement image first writes the forwarded response (if any), then
   processes the forwarded in-flight request (if any), then enters the normal
   read loop. SQLite WAL recovers any unfinished state on the fresh open
   (same as the orphan-watcher exit path, #547).
5. On handoff failure (exec/spawn error) the loop **keeps serving** on the
   stale image: the fail-loud gate stays active and the error is logged —
   never a silent exit.

### 4. Automatic handoff (opt-in, #1045)

`PERSEUS_VAULT_AUTO_HANDOFF=1` replaces the fail-loud refusal with a
transparent swap: a stale-image `tools/call` is intercepted, the serialized
request is forwarded (`PERSEUS_VAULT_HANDOFF_PENDING_REQUEST`), the swap runs
*instead of* a response, and the replacement image answers that very request.
The client sees one clean response from the new binary — no reconnect tool
invocation, no session restart, no error surfaced. Requests over 24 KiB fall
back to the loud `isError` path (Windows env blocks cap at 32KB). The
handoff tool and `perseus_vault_health` remain exempt.

## Supported local workflow (macOS / Rovo Dev)

```bash
# 0. (optional) make rebuilds seamless:
#    PERSEUS_VAULT_AUTO_HANDOFF=1 perseus-vault serve --db ...   # transparent swaps
#    or keep the default fail-loud mode and use the explicit tool.

# 1. rebuild
cargo build --release

# 2. (optional) dry-run report: binary_stale=true, would_handoff=true
#    via any MCP client: perseus_vault_handoff_restart {"dry_run": true}

# 3. hot-swap the live session — the next response comes from the new binary
perseus_vault_handoff_restart {"confirm": true}
```

Constraints:

- **Unix/macOS only**: Windows locks a running executable, so the on-disk
  binary cannot be replaced mid-session there at all (staleness cannot
  arise); update across a session boundary.
- **Do not pipeline requests during handoff** in fail-loud mode: the
  old image's stdin `BufReader` may hold read-ahead bytes. MCP clients are
  strictly request/response, so a compliant client is unaffected; the
  forwarded-request/response contract closes the in-flight gap that
  #858 left open. In `PERSEUS_VAULT_AUTO_HANDOFF` mode the swap answers the
  triggering call itself, so nothing is lost.
- If a handoff fails (e.g. the binary was deleted rather than replaced), the
  server logs the failure and keeps serving loud errors; restart the client
  session to recover.
- Diagnostics override: `PERSEUS_VAULT_IGNORE_STALE_BINARY=1` bypasses the
  fail-loud gate (stale results are still served).

## Verification

- `live_update` unit tests: in-place rewrite, rename-replace, deletion,
  `" (deleted)"` suffix stripping, gate matrix, all four report states,
  pending-flag roundtrip, handoff-state env roundtrip + sanitization,
  pending-request/pending-response stash, cap, and clear semantics.
- `mcp` unit tests: forwarded state restores `initialized` + agent identity;
  a partial state without the init flag does not authorize the session.
- **End-to-end** (`tests/live_update_handoff.rs`, Unix): drives the REAL
  binary over stdio — initialize → rebuild → loud refusal → handoff →
  session continues on the new image (`binary_stale=false`, `tools/list`
  works, no `-32002`) → repeat handoff cycle; and the auto-handoff scenario
  where the replacement image answers the intercepted call directly. These
  are the tests that would have caught #1045 before it shipped — the #858
  exec path was previously covered only by report-state unit tests.
