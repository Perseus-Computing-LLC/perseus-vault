# Planning-Boundary Context Pack and Delegation Brief

Status: implemented (#1039)
Grounded in: r/ContextEngineering "Mine your existing intent artifacts before
building more memory" (bnunamak, 2026-08-14); DevSpecs (MIT,
github.com/devspecs-com/devspecs-cli); internal subagent-briefing practice.

## Summary

The planning boundary is a distinct retrieval joint: when an agent is about
to decide what work means, it needs a **small reconstruction of the relevant
development narrative** — why something exists, what was rejected, what is
binding, what is next — not the 30 most semantically similar chunks and not a
continuous memory blob. This spec defines the two surfaces that serve that
joint:

1. **Enriched `perseus_vault_handoff_pack`** — the existing budget-bounded,
   lifecycle-filtered context pack gains opt-in planning sections: an
   **intent trail** (recent journal events tied to the packed entities) and
   **next work** (journal forward plans + recall_when anticipation matches),
   plus pack-scoped **contradiction flags**.
2. **`perseus_vault_delegation_brief`** — the same machinery rendered as a
   deterministic markdown handoff packet (goal, scope, binding context,
   do-not-resurrect list, intent trail, next work, output contract) that a
   parent agent can hand to a subagent without inheriting the chat session.

Boundary discipline: generation happens at the planning boundary. Preload /
continuous preparation remains the #875 anticipation domain (self-tuning
recall_when); this surface is the pack composition + export joint.

## Problem

- An agent planning a change to a subsystem needs the architectural decision
  that established authority, the last plan that touched it, the evidence
  from implementing it, the subsequent decision that changed a constraint,
  and whatever active work currently depends on it — an intent-graph
  traversal, not document similarity.
- Time and authority matter almost as much as similarity: a rejected proposal
  and the decision that killed it can both be excellent embedding matches.
  The rejection lives *between* artifacts (the supersede link graph), which
  no chunk-level index can see.
- Repo intent artifacts are passive; the correction path lives in the store's
  governed entities, journal, and supersession graph. The pack must surface
  the *binding* slice, not the similar slice.

## Proposal

### handoff_pack enrichment (opt-in, backward compatible)

New arguments (all default false/5, output byte-identical to the pre-#1039
contract when unset):

- `include_intent_trail: bool` — add `intent_trail`: journal events whose
  `entity_id` is in the pack candidate set, or whose category matches a
  candidate category (non-entity-anchored events). Newest first,
  deterministic id tie-break, clipped (800 chars per field), bounded by
  `max_trail` (1..=20, default 5).
- `include_next_work: bool` — add `next_work`:
  - `forward_plans`: intent-trail events with non-empty forward (the
    journal's evaluated/acted/**forward** contract is the vault-native
    "next bounded piece of work" slot), newest first, ≤ 5.
  - `anticipation`: entities whose `recall_when` triggers match the query
    (active work that depends on this decision area), visibility-gated
    (#996), ≤ 5.
- `include_conflicts: bool` — add `conflicts`: pack-scoped contradiction
  flags from the existing conflict detector, run per distinct pack category
  (≤ 3 categories), keeping only pairs where both members are inside the
  candidate set. The full pre-injection contradiction surface stays gated on
  #917 (MemConflict replication).
- `workspace_hash` now **threads into recall** (scope fix): a scoped pack
  cannot pull another workspace's decisions into the planning boundary.
  Unset keeps the legacy unscoped behavior.

### delegation_brief

`perseus_vault_delegation_brief(query, goal, output_contract?, budget_tokens=4000, include_expired=false, workspace_hash?)`

Deterministic markdown sections: header (goal/scope/pack budget + digest) →
**Binding context** (packed entities, clipped) → **Rejected or superseded —
do not resurrect** (superseded exclusions listed) → **Intent trail (recent)**
→ **Next work** (forward plans + recall_when) → **Output contract** →
**Sources** (entity ids). Same digest contract as handoff_pack. Validation:
query and goal required non-empty; budget 200..=100000.

### Scope and authority

Both surfaces are in the MCP scoped-read set (profile↔workspace binding
enforced, #879). Read-only; no mutation of any kind. Identity stamping and
visibility gates apply as for recall.

## Contract

- `perseus_vault_handoff_pack` output adds `intent_trail`, `next_work`,
  `conflicts` only when requested; `pack_digest` covers the pack only (stable
  contract).
- `perseus_vault_delegation_brief` returns `{brief, brief_digest, budget,
  excluded, read_only: true}`.
- Determinism: every list is explicitly sorted (trail: created_at desc, id
  desc; conflicts: category asc, detector order; anticipation: trigger-match
  order); identical store state → identical output string.

## Success criteria (from #1039)

- Given an anchor with ≥ 1 superseded decision, the pack lists the
  superseding entity as binding and the superseded one as historical
  (excluded with reason `superseded`), regardless of embedding similarity.
- Pack generation is deterministic for the same anchor + store state, bounded
  by default (< ~4k tokens), and includes the next-bounded-work field when
  present (forward plans + anticipation).
- Delegation-brief export round-trips: a subagent given only the brief can
  reconstruct goal, scope, binding decisions, and output contract.
- No regression on recall_when preload latency (enrichment is opt-in and
  read-only; recall_when is invoked only for `include_next_work`).

## Cross-references

- #875 — Learned anticipation / recall_when self-tuning (closed): the
  preload/anticipation complement; this surface is pack composition.
- #917 — Pre-injection contradiction-flag surface (open): full surface gated
  on MemConflict replication; `include_conflicts` is the bounded precursor.
- #996 — retrieval-leak harness: visibility gate reuse in anticipation.
- autonomous-ai-agents/subagent-briefing (internal skill): the handoff-packet
  shape this surface automates.
