# Supersession Impact Index — design (perseus-vault #1029)

Status: v1 implemented (schema v46, `perseus_vault_impact_report`).
Source: "Beyond Memory: A Transactional Continuity Kernel for Long-Lived AI
Agents" (arXiv:2608.11632) — the effect-continuity layer the paper leaves
open (§3.5: CK atomicity ends at the state boundary; external effects are
idempotent-outbox intents only).

## Problem

The vault records `derived_from` citations on entities and (since v46)
`justification_entity_ids` on authorized actions, but nothing propagates a
supersession/retraction to the entities and actions that derived from the
changed fact. When justification changes, dependents are silent.

## Scope

1. Reverse impact closure: for a superseded/retracted fact, enumerate the
   decisions/actions that cited it as grounding (direct + transitive).
2. AAR integration: flag PENDING actions whose cited justification changed
   so the pre-execution authority check can re-validate freshness.
3. Completed actions with changed justification: review suggestions only —
   external effects are irreversible; we can only flag.
4. Bounded closure (depth/age caps) and as-of semantics.

## Design decisions

### Lazy vs eager: LAZY at read time (v1)

The reverse index is computed at report time by walking `entities.links`
(`derived_from` edges, via `json_each` + `json_valid` guard) and
`authorized_actions.justification_json`. Rationale:

- Write paths stay free of index-maintenance coupling (no new failure modes
  on cite/supersede/retract).
- The closure is naturally transaction-consistent with whatever the store
  looks like now; an eager materialized index would need its own
  transactional update discipline and rehash logic.
- Cite/supersede volume in this vault is low; a full closure scan is bounded
  by the LIMIT + depth/age caps below.

Eager materialization (updated on cite/supersede/retract) is the documented
optimization path if report latency ever matters.

### Boundedness

- `depth_cap` (1..16, default 3): maximum `derived_from` hop depth.
  Frontier expansion beyond the cap marks `bounded_closure.truncated=true`
  and stops (it never silently pretends the closure is complete).
- `age_cap_days` (1..36500, default 365): dependents older than the
  watermark are excluded from the report (they are too stale to still
  matter for re-validation) — they are still VISITED for closure
  continuation (a young dependent may cite an old one), but not listed.
- Per-node scan LIMITs (2000 entities / 500 actions) bound pathological
  fan-out.

### as-of semantics (v1)

`as_of_unix_ms` computes the report at a past transaction instant by
filtering dependents and pending actions on their creation time
(`created_at_unix_ms <= as_of`). This gives a correct *structural* closure
for any T because:

- a `derived_from` edge is created with its entity and never mutated in
  place for the citation's target (unlink/supersede create new history
  rows);
- pending-action filtering by creation time is exact.

Known v1 limitation (documented, not hidden): an entity that was ALREADY
superseded/retired before T is still visited, because v1 does not replay
`entity_history` to establish existence-at-T for every visited node. Full
bi-temporal closure (existence-at-T via `entity_history`, combining with
the existing `valid_at`/`as_of` temporal machinery) is the follow-on step.

### AAR integration

- `action_intent` gained `justification_entity_ids` (max 64, each must
  reference an existing row — fail-closed, mirroring the derived_from write
  validation). Stored as `authorized_actions.justification_json`.
- The report classifies citing actions:
  - `intent` / `approval_requested` → `pending_actions` with the AAR review
    flag (the note spells out: re-validate freshness before execution);
  - `executed` / `failed` → `completed_actions` (review suggestions, never
    automatic reversal);
  - `denied` / `cancelled` → ignored (nothing will execute).
- The pre-execution authority check re-validation itself (lease acquire
  flagging stale justification) is the follow-on; the report is the surface
  the operator/agent consults first.

## Data model (v46)

`authorized_actions.justification_json TEXT NOT NULL DEFAULT '[]'` —
additive ALTER-appended column (same physical position on fresh and
migrated stores; `authorized_actions` is hydrated by index, never
`SELECT *`).

## Tool surface

`perseus_vault_impact_report` — inputs: `entity_id` | (`category`, `key`),
`depth_cap`, `age_cap_days`, `as_of_unix_ms`. Output: target, dependents
(ordered by authority = importance, then recency), pending_actions,
completed_actions, bounded_closure metadata.

## Tests

`impact_report_lists_dependents_and_flags_actions`:
- E2E: fact F cited by two decisions (direct) + one transitive decision +
  one pending action + one completed action → supersede F → report lists
  all three dependents with correct dependency depth/kind, flags the
  pending action, lists the completed one.
- Bounded closure: depth cap excludes the depth-2 dependent and marks
  truncation.
- as-of: an instant before everything yields empty dependents and empty
  pending actions.
