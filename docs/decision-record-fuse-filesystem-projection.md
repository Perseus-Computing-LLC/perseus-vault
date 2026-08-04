# Decision record: FUSE filesystem projection of the Vault memory store

Status: decision record — evaluation only, no implementation commitment
Date: 2026-08-04
Resolves: #841
Related: MCP resource surface design (#842) ·
[Evidence chains and continuous attestation](evidence-chain-guidance.md)

## Decision

**Keep the MCP resource surface as the read surface. Do not add a FUSE
filesystem projection at this time.** A FUSE projection remains a candidate
*additional* read surface (agents and unix tools reading memory as plain
files, no MCP client needed), but only under conditions that are not met
today — see [Follow-up gates](#follow-up-gates). This is a conditional
non-adoption, not a rejection of the pattern.

## Context

Perseus Vault already serves context through the MCP resource surface, and the
resource-surface design in #842 is adopting a write-boundary layout (per-entity
resources + a workspace index acting as notification fan-in + an RFC6570 URI
template). The external ecosystem (ley-line-open + mache/cloister) exposes
structured intelligence as a FUSE filesystem over a content-addressable
SQLite projection, letting unix tools and agents read it as plain files. The
open question is whether Vault should offer the same *additional* surface.

## Read-surface comparison

| Dimension | MCP resource surface (current direction, #842) | FUSE filesystem projection |
|---|---|---|
| Transport | MCP protocol over the existing server channel | OS VFS syscalls through a mount point (fuse kernel module + userspace daemon) |
| Read pattern | `resources/read` on named URIs; index-as-fan-in for change-driven re-reads; list via `resources/list` + template | arbitrary `open`/`readdir`/`stat`; tools that assume POSIX paths work unmodified |
| Latency | RPC round-trip per read; per-entity granularity bounds re-read cost | syscall-level access after daemon resolve; per-entity files bound re-read cost; but path resolution + authority check per syscall |
| Tooling compatibility | Any MCP client; no unix-tool story | Plain `cat`/`grep`/`find`/editors; shell pipelines work |
| Authority/visibility | Enforced in the server on every read path (workspace/visibility/agent filters) | Must be enforced in the FUSE daemon for every path — the kernel does not know Vault authority |
| Air-gap / offline | Works wherever the server runs; no mount namespace needed | Requires fuse device + mount privileges; a broken mount or missing daemon fails all reads |
| Freshness | Push notification channels (`resources/updated`, `resources/list_changed`) + explicit reads | Reads hit the daemon live (no push model); staleness governed by daemon cache policy |
| Attack surface | One protocol surface, already governed | Kernel module + daemon + mount-point path handling; larger surface, new failure modes |

## Security boundary (mandatory)

A FUSE projection, if ever added, **must enforce the same authority and
visibility rules as every other read path, with no bypass via file access**:

- every path lookup, `readdir`, `read`, and `stat` goes through the same
  workspace/visibility/agent authorization checks as the MCP read path — the
  mount is a view, never a back door;
- deny by default: paths outside the caller's authorized view do not exist
  (ENOENT), never fall back to unfiltered content;
- the daemon holds no more privilege than the server; mount credentials are
  per-caller, and a daemon crash yields a closed mount, not an open one;
- the byte-integrity boundary (#835) applies unchanged: file contents are
  byte identity only; supersession/validity/authority state is surfaced as
  metadata or inode-level state, never implied by the digest.

This boundary is a precondition for any FUSE work, not an afterthought.

## Non-goals

- No implementation commitment: this issue produces a decision record, not a
  FUSE implementation.
- No performance claims: nothing here asserts that a FUSE projection is
  faster or cheaper than the MCP surface. Any such claim requires a bounded
  measurement plan first.
- FUSE is not a replacement for the MCP resource surface; it would be an
  additive surface only.

## Rationale

1. The MCP surface already covers the primary consumers (agents via MCP
   clients) and is being given a sound granularity model (#842); adding a
   second read surface now would split the read-path contract before the
   first one is productionized.
2. FUSE's real advantage — unmodified unix tools — is valuable but unmeasured
   for Vault's workload; the comparison above is qualitative and explicitly
   not a scaling claim.
3. The security boundary is substantial: authority enforcement must be
   re-implemented in the daemon, with new kernel-level attack surface. That
   cost is only justified by measured tooling demand.
4. Air-gap and deployment posture: the MCP surface runs inside the existing
   process with no mount privileges; FUSE requires fuse device access, which
   complicates the container/server story Vault currently targets.

## Follow-up gates

Reopen this decision (conditional adopt) only when all of the following hold:

1. The #842 MCP resource surface (per-entity resources + index-as-fan-in) is
   shipped and in use.
2. A bounded measurement plan (read amplification, latency, tooling demand)
   shows a concrete need the MCP surface cannot meet — no claims without
   measurement.
3. The security boundary above is specified to implementation level and a
   review confirms no authority/visibility bypass is possible via the mount.
