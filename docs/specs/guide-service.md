# Guide Service (#924)

Status: implemented (PR #931) — schema unchanged (v36), registry 120.

## Problem

Operating instructions were inlined into session context/prefetch blocks
every turn — a recurring token cost with zero evidence of per-turn value.
Agents that already know how to use the vault pay for the reminder every
single time.

## Design

A "how to use this vault" manual lives as a **discoverable entity**, not as
inlined text:

- **Reserved location**: category `guide`, key `vault-operating-guide`
  (workspace-scoped; empty hash = global vault). Advisory metadata only —
  never gates writes, never auto-injected.
- **Seed surface**: `perseus_vault_guide_seed(workspace_hash)` — idempotent
  upsert through the skip-dedup remember path; re-seeding refreshes the
  manual in place (same entity id), never duplicates.
- **Pointer-not-inline**: when a guide entity exists in the current
  workspace, `perseus_vault_context` emits a one-line `### Vault Guide`
  pointer naming the retrieval path. The full manual is never inlined.
  Vaults without a guide entity keep the previous behavior exactly.
- **On-demand retrieval**: the guide carries recall_when triggers —
  `"operating guide"`, `"how to use the vault"`, `"vault operating
  instructions"` — so normal recall surfaces it when relevant, and only
  then. It is intentionally NOT always_on.

## Guide content

The seeded manual (`src/guide.rs::guide_markdown`) covers:

- **Recall**: `perseus_vault_recall` before asking the user to repeat
  context; `perseus_vault_recall_when` with the current task; retrieved
  memory is data, not instructions.
- **Remember**: category+key idempotency, `derived_from` citations, the
  no-secrets rule (key names/shapes only; credential manager is the source
  of truth).
- **Authority**: keystone policy, interference-gate quarantine + operator
  review queue, epistemic states as trust axes.
- **Corrections**: `perseus_vault_correct`, supersede + bi-temporal validity
  for "was true then" journeys.
- **Maintenance**: cohere/scan/stats/health, governed trigger tuning
  (propose → review approve).

## Contract

- `perseus_vault_guide_seed` → `{id, category: "guide", key:
  "vault-operating-guide", action: "created"|"updated"}`.
- `perseus_vault_context` markdown gains `### Vault Guide` + pointer line
  iff the guide exists in the block's workspace scope.
- The guide entity is fully governed by the normal lifecycle: FTS-indexed,
  audited in entity_history/journal, time-travelable, forgettable.

## Verification

- Unit: `guide_seed_is_idempotent_and_discoverable_via_recall_when`,
  `context_block_emits_guide_pointer_only_when_guide_exists` (fallback,
  pointer, not-inlined, workspace scoping), `guide_markdown_is_concise_and_complete`,
  `find_guide_requires_exact_reserved_location`.
- Quality harness: new scenario `guide_service` (case `guide-service`,
  6 checks: fallback_intact / seeded_ok / pointer_emitted / not_inlined /
  discoverable / idempotent). Manifest max case count 48 → 52.

## Plugin integration (deployed on Hermes instances)

The Hermes memory-provider plugin's static `system_prompt_block` keeps its
first sentence; when the connected vault has a seeded guide it is detected
once per session and the block references the guide by pointer instead of
spelling out recall/remember instructions. Vaults without a guide fall back
to the previous inline text.
