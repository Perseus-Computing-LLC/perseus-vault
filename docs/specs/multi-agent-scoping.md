# Feature Spec: Multi-agent scoping — agent_id + trust tiers

**Status:** implemented (#684) — core; enumerated follow-ups pending
**Depends on:** `agent_id` on entities/journal (v1.2.0), the crypto audit chain, keystones (#683)
**Competitive driver:** MemClaw's eToro case study — 300+ agents sharing memory with governed access — is the 2026 production multi-agent pattern. We match it local-first, deterministic, and auditable.

## What already existed

`entities` and `journal` have carried `agent_id` since v1.2.0, `agent_id` is
folded into the audit-chain payload commitment (provenance), and a per-entity
`visibility` column existed — **but was stored and never enforced.** Full
workspace-scoping machinery (unique identity, scope-weight widening,
workspace-bound audit chain) was also already present.

## What #684 adds

### Agent registry + trust tiers
`agents` table (schema v22): `agent_id`, `name`, `trust_tier` (0-3), `fleet_id`,
timestamps. The `mimir_agent` tool registers/updates (pass `trust_tier`) or looks
up (omit it). Tier model:

| Tier | Capability |
|---|---|
| 0 | read own memories only |
| 1 | read fleet memories |
| 2 | read all + author keystones |
| 3 | admin |

`agent_trust_tier(id)`: empty id → **3 (unscoped/admin)** so single-agent
deployments are unaffected; registered → its tier; unknown non-empty → 0.

### Visibility enforcement (`can_read`)
The stored `visibility` field is now enforced on recall:

- `private` → author (or admin) only
- `fleet` → same fleet, or tier ≥ 2, or admin
- `workspace` / `tenant` / `''` (the default) → everyone

### Session identity capture
The MCP transport captures `clientInfo.name` from the `initialize` handshake
into `MCPState.session_agent_id` and stamps it onto every tool call as
`requesting_agent_id`. `handle_recall` drops entities failing `can_read` before
any reconstruction/reinforcement/return — so scoping is transparent (no explicit
arg) and hidden entities are never even reinforced. This is distinct from the
existing `agent_id` recall arg, which is an author **filter**, not the requester
identity — conflating them would break "a tier-1 agent reads fleet-mates'
memories."

### Keystone authoring gate (completes #683)
`keystone_set` now uses the **authoritative registered tier** when the author is
a known agent (non-spoofable), falling back to the caller-asserted
`author_trust_tier` otherwise. `trust_enforced` in the response reports which
mode applied.

## Non-breaking guarantee

Enforcement engages only for identified, lower-tier requesters against
`private`/`fleet` entities. Empty/unknown requester → unscoped; default
`workspace` visibility → visible to all. Every existing entity and benchmark is
therefore unaffected (verified: full lean suite green).

## Acceptance criteria (#684)

- [x] `agents` table with `trust_tier` + `fleet_id`
- [x] `visibility` + `agent_id` on memories (pre-existing) now **enforced**
- [x] Recall filtered by visibility scope (`can_read` on the primary recall path)
- [x] Trust-tier gating on a sensitive op (keystone authoring)
- [x] `agent_id` stamped on ops for provenance (pre-existing audit commitment)
- [x] Docs: this "Multi-Agent Memory" section
- [ ] Enforcement on the remaining read surfaces (`ask` / `global_recall` /
      `memories`) — follow-up
- [ ] Automatic author `agent_id` population from the session on writes —
      follow-up (kept explicit here to avoid colliding with the author-filter arg)
- [ ] Per-agent recall tuning (retrieval profiles) — follow-up

## Follow-ups (why deferred)

Author auto-population and full read-surface coverage were deliberately deferred
rather than rushed: the visibility model is security-sensitive, and the primary
`recall` path is where it matters most and is fully testable now. The remaining
read paths reuse the same `can_read` primitive, so the follow-up is mechanical.
