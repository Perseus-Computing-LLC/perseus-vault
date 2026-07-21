# Memory taxonomy and default precedence model

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #720 (parent), #723, #724, #719 · Informs: #717

This document defines Vault's canonical memory classes, the required metadata
per class, the default precedence model used when memories are *served* (as
opposed to merely recalled), and the override behavior. It is a product
contract: retrieval and context-assembly implementations are expected to
converge on it, and deviations are bugs or documented overrides.

## 1. Canonical memory classes

Vault already stores every class below. The taxonomy makes the classes
*explicit* so that serving, ranking, consolidation, and retention can treat
them differently on purpose.

| Class | Definition | Maps to today | Examples |
|---|---|---|---|
| `instruction` | A directive the operator expects the agent to follow. Scoped (global / workspace / repo). | entities with `always_on=true`, `recall_when` triggers, convention category | "Never store secrets in the Vault", "restart gateway via s6-svc" |
| `preference` | A stable statement about how the user wants things done. Not an order, but a default. | user-profile style entities, preference-tagged insights | "prefers adversarial competitive intel", "concise responses" |
| `correction` | A record that a prior approach or belief was wrong, with the right behavior. | `correction` category via `mimir_correct` | "BWS short values are stale keys, not truncation" |
| `episode` | A dated account of what happened in a session or operation. Ephemeral-to-working by nature. | `capture` category, journal entries | "Plutus redeploy 2026-07-20 went 21:55Z→23:19Z" |
| `observation` | A consolidated, evidence-tracked fact merged from overlapping sources. | `observation` category via `mimir_consolidate` | merged Stripe hygiene facts |
| `semantic` | A durable fact about the world or the estate, not tied to one episode. | `knowledge`/`operations` categories | "Mneme auth is a single static bearer token" |
| `insight` | A synthesized higher-order pattern derived from other memories. | `insight` type, `mimir_dream` outputs | "deploy windows drop Stripe webhooks" |
| `anchor` | A pointer from a memory to an external system of record. | links, `external_refs` (see provenance spec) | Jira key, repo/PR, file path, session id |
| `belief` *(overlay class)* | A claim plus its evidence, confidence, scope, and supersession state — see §5. | derived over existing entities | "GitHub auth from Cloud works" |

Class assignment is recorded in the entity's existing `type` / `category`
fields; no storage migration is required. New writes SHOULD declare a class;
existing entities are classified by the mapping above.

## 2. Required metadata by class

Minimum metadata each class must carry for serving to be explainable.
"Required" means a serving view may assume the field exists (possibly null).

| Class | Required metadata |
|---|---|
| instruction | `scope` (workspace_hash or global), `recall_when` triggers *or* `always_on`, `created_at`, `status` |
| preference | `scope`, `subject` (who/what the preference is about), `created_at` |
| correction | `wrong_approach`, `user_correction`, `task_context`, `recorded_at`, optional `valid_from` |
| episode | `recorded_at`, `source`, `session_id` where available |
| observation | `evidence` (source entity ids), `proof_count`, `recorded_at` |
| semantic | `category`, `key`, `certainty`, `valid_from`/`valid_to` where time-bound |
| insight | `derived_from` (evidence ids), `derivation` (`dream`/`consolidate`/`agent`), `certainty` |
| anchor | `ref_type`, `ref_value`, `relationship` (see `memory-provenance-and-external-refs.md`) |
| belief | `claim`, `scope`, `confidence`, `support_count`, `supporting_entity_ids`, `superseded_by`, `last_revalidated_at` |

## 3. Default precedence model

When several memories compete for a finite serving budget (context
injection, briefing slots), serve in this order:

1. **Corrections** — a correction is the operator explicitly rewriting the
   record; it always outranks what it corrects.
2. **Scoped instructions** — narrower scope wins: repo > workspace > org > global.
3. **Stable preferences** — long-lived, repeatedly confirmed.
4. **Recent episodic evidence** — fresh, directly observed episodes.
5. **Synthesized semantic insights** — durable but one step removed from evidence.
6. **Background memory** — everything else, by relevance.

Within a tier, rank by the composite relevance score (lexical, semantic,
freshness, decay, trust — see #718's explanation contract and the Perseus
composite-ranking spec).

### 3.1 Scope and freshness rules (codifies #719)

These rules are absolute, not tie-breakers:

- **R1 — Narrower scope beats wider scope.** A same-repo fact outranks a
  team-wide belief; a workspace instruction outranks a global one.
- **R2 — Fresh local fact beats stale global belief.** A newer fact in the
  current scope outranks an older generalized belief *even when the belief
  has higher semantic similarity to the query*. Similarity ranks within a
  scope tier, never across the freshness/scope boundary.
- **R3 — Direct evidence beats inference.** An asserted/observed memory
  outranks an inferred or synthesized one on the same subject (see the
  provenance spec for `memory_kind`).
- **R4 — Non-superseded beats superseded.** A superseded entity is never
  served as current fact; it may only appear in history, audit, or
  contradiction views.
- **R5 — Conflict without supersession surfaces, not resolves silently.**
  Two live contradictory memories are both served to a contradictions view
  rather than one being arbitrarily preferred.

### 3.2 Overrides

- **Explicit pin** (`always_on=true`) bypasses precedence and is served in
  every relevant context, hard-capped by the context budget.
- **Operator directive** in-session ("forget X", "prefer Y") is recorded as
  a correction (tier 1) and therefore wins by construction.
- **Recall-time demotion**: `min_decay`, `valid_at`/`as_of` filters, and
  `include_archived=false` constrain the candidate pool *before* precedence
  is applied; they never reorder the precedence tiers themselves.
- An override must be inspectable: serving explanations (see
  `served-memory-api.md`) must name the override when one fired.

## 4. Interactions with consolidation, correction, and supersession

- **Consolidation** (`mimir_consolidate`, `mimir_dream`) produces
  `observation` / `insight` class entities. Sources keep their class; the
  product's class is the *highest-precedence class among its sources*,
  capped at insight (a consolidation never manufactures a correction or
  instruction).
- **Correction** creates a tier-1 entity and SHOULD supersede the entity it
  corrects (`mimir_supersede`), which closes the old entity's valid period
  and demotes it per R4.
- **Supersession** changes the *active view* while preserving history:
  superseded entities remain queryable via `as_of`/`valid_at` and appear in
  audit views, but are excluded from default serving by R4.
- **Promotion** (buffer → working → semantic layers) never changes class;
  it changes decay resistance only.

## 5. The belief overlay class (informs #717)

A *belief* is not a new storage entity; it is a **derived view** over the
existing store that presents one claim as a first-class object:

```
belief := {
  claim:                  string            # canonical statement
  scope:                  workspace | global
  confidence:             0.0–1.0           # rolled up from certainty + verification
  support_count:          int               # distinct non-merged supporting entities
  supporting_entity_ids:  [entity_id, ...]  # the evidence
  last_revalidated_at:    timestamp
  superseded_by:          belief_id | null
  contradicted_by:        [belief_id, ...]
}
```

Derivation rules:

- Entities linked `evidence_for` / `derived_from` the same subject fold
  into one belief; `support_count` is the cardinality of that set.
- A `correction` on the subject creates a successor belief and sets
  `superseded_by` on the old one; `mimir_supersede` provides the storage
  primitive.
- `last_revalidated_at` updates on any retrieval reinforcement of a
  supporting entity.
- Retrieval MAY prefer beliefs by scope, confidence, freshness, and
  support_count, and MUST be able to explain that preference (the
  explanation fields are defined in `served-memory-api.md`).

Implementation of the overlay (materialization, indexing, and the
`belief`-shaped recall mode) is follow-up engineering tracked separately;
this spec freezes the semantics so the implementation cannot drift from the
precedence model.

## 6. API and storage implications

- **No storage-engine change and no schema migration.** Classes map onto
  existing `type`/`category`; belief is derived, not stored.
- `mimir_remember` SHOULD accept an optional `memory_class` hint; absent
  that, classifiers map from category/type as in §1.
- Recall/context tools SHOULD surface `memory_class` and the precedence
  tier actually applied, behind an opt-in explanation flag (see
  served-memory spec).
- The precedence model is a pure function over (class, scope, freshness,
  supersession, relevance); it is unit-testable without a database.
- Precedence constants (tier order, scope order) live in one documented
  config block so deployments can re-order tiers without code changes.

## 7. Acceptance checklist traceability

- #720: taxonomy (§1), precedence (§3), interactions (§4), API/storage
  implications (§6). ✔
- #723: canonical classes (§1), required metadata (§2), storage/API
  implications (§6). ✔
- #724: default precedence (§3), overrides (§3.2), correction/supersession
  interactions (§4). ✔
- #719: R1–R4 codify fresh-local-beats-stale-global and predictable
  demotion of superseded knowledge (§3.1). ✔
