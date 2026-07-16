# Feature Spec: Keystones — mandatory policy rules

**Status:** vault side implemented (#683); render directive pending in the Perseus orchestrator
**Depends on:** journal/audit chain (crypto-chained mutations); trust tiers (#684) for enforced authoring
**Competitive driver:** MemClaw's "Keystones" are their #1 differentiator — mandatory, deterministic, merged-by-scope policy rules an agent structurally obeys over conflicting instructions.

## Problem

Ordinary memories are *retrieved when relevant* — probabilistic, rank-ordered,
and subject to decay/compaction. Some directives must not work that way:
"Every memory write MUST carry a retention class", "Customer PII MUST NOT cross
agent boundaries", "Cite source memory IDs in every briefing". These are
policy, not recall: they must be fetched **deterministically** at session start,
**merged** across organizational scope, and **obeyed** over any conflicting
instruction — and they must survive context compaction.

## Design (vault side, this repo)

A dedicated `keystones` table (not `entities`), because keystones have policy
semantics, not memory semantics (no decay, no relevance ranking, no dedup
merge):

| Column | Meaning |
|---|---|
| `id` | `ks-<uuid16>` |
| `content` | the rule text |
| `scope` | `tenant` \| `fleet` \| `agent` |
| `scope_id` | fleet_id / agent_id (empty = scope-wide) |
| `weight` | conflict resolution — higher wins |
| `trust_tier_required` | minimum authoring tier (default 2) |
| `workspace_hash` | workspace scope (empty = global) |
| `author_agent_id` | provenance |
| `created_at` / `updated_at` | — |

`UNIQUE(scope, scope_id, content, workspace_hash)` makes re-setting the same
rule an in-place update, not a duplicate.

### Tools

- `mimir_keystone_set(content, scope, scope_id?, weight?, trust_tier_required?, author_trust_tier?, agent_id?, workspace_hash?)`
  → `{ id, created, trust_enforced }`. Every mutation is appended to the
  cryptographic audit chain (`event_type = keystone_set`, `category = keystone`).
- `mimir_keystone_get(scope?, scope_id?, workspace_hash?)` → `{ keystones[], count }`,
  ordered **weight DESC, then scope (tenant < fleet < agent), then id** for
  deterministic precedence. Widening filters: an omitted scope/scope_id matches
  all; a provided `workspace_hash` includes global (`''`) keystones; a provided
  `scope_id` includes scope-wide (`''`) rules.

### Trust-tier gating (#684 dependency)

Authoring requires tier ≥ `trust_tier_required` (default 2 — "write keystones"
in #684's tier model). Until #684 provides a per-agent trust registry and
transport-level session identity, `author_trust_tier` is **caller-asserted**:
when supplied it is enforced (a lower tier is rejected); when omitted the write
proceeds and the response sets `trust_enforced=false` so callers know
enforcement is pending. When #684 lands, the caller-asserted value is replaced
by the authenticated agent's tier and enforcement becomes automatic.

## Out of scope for this repo (Perseus orchestrator)

The **`@keystone` render directive** — injecting the merged keystones into
`AGENTS.md` / the system prompt at session start, ahead of all other context —
lives in the separate Perseus orchestrator (the `perseus_*` tools), which owns
the render pipeline (`perseus_get_context`, `perseus_prompt`, the `Prepare`
pre-turn injection). This repo (the storage + recall backend) supplies the
deterministic `keystone_get` query surface that directive renders from. The
orchestrator work is tracked separately.

## Acceptance criteria (this repo)

- [x] `keystones` table with scope + weight (schema v21).
- [x] `mimir_keystone_set` / `mimir_keystone_get` MCP tools.
- [x] Trust-tier gating on authoring (caller-asserted pending #684).
- [x] Keystone mutations crypto-chained like other vault ops.
- [x] Deterministic weight/scope ordering for a renderer to consume.
- [ ] `@keystone` render directive — **Perseus orchestrator, separate repo.**
- [ ] Enforced (non-asserted) trust gating — **gated on #684.**
