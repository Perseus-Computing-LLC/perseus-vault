# Served-memory APIs for synthesis and explainable recall

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #722 (parent), #726, #718
Related: `memory-taxonomy-and-precedence.md` (classes, precedence),
`source-anchors-corrections-retention.md` (anchors, policies)

Raw recall answers "what memories match this query?" Served memory answers
"what should the operator/agent know right now, and why?" This spec defines
Vault's served-memory views, the explanation payload every served item
carries, how serving relates to existing recall APIs, and the validated
briefing flow.

## 1. Served-memory views

A *view* is a named, purposeful projection over the memory store. Views are
computed at request time from the same entities recall uses; nothing is
denormalized into separate storage.

| View | Purpose | Contents |
|---|---|---|
| `active_instructions` | What rules are in force right now | instruction-class memories in scope order (repo > workspace > global), corrections affecting them |
| `relevant_context` | What matters for this task | precedence-filtered mix of preferences, semantic facts, fresh episodes for a query/task |
| `contradictions` | What disagrees with itself | live conflicting pairs (taxonomy rule R5), stale assumptions past `valid_to`, unmerged duplicates |
| `briefing` | What to read before acting | narrative-ordered bundle: active instructions → corrections → current state → open loops → contradictions |
| `recent_decisions` | What was decided lately | decision/operations entities ordered by recency with supersession status |

A view request names the view, the scope (workspace_hash), an optional
query/task, and a budget (max items / max chars — serving is always
budget-limited, matching the context-injection posture).

```
serve(view="briefing", query="plutus status", workspace_hash="…",
      max_items=12, explain=true) -> ServedMemoryView
```

## 2. Explanation payload (every served item)

Explanations are the product. A served item without its explanation is a
bug; explanations must be compact enough for prompt injection and debugging.

```json
{
  "entity_id": "mem-2a806a471f4d",
  "content": "…",
  "memory_class": "episode",
  "explanation": {
    "why_served": "briefing view: current state; tier-4 recent episodic evidence",
    "matched_on": ["lexical: 'plutus'", "semantic similarity 0.81"],
    "scope": "global",
    "anchors": [{"ref_type": "pull_request",
                 "ref_value": "github:Perseus-Computing-LLC/plutus#176"}],
    "recorded_at": "2026-07-20T00:50Z",
    "last_reinforced_at": "2026-07-20T00:50Z",
    "confidence": 0.72,
    "support_count": 3,
    "supersession": "active",
    "retention_policy": "decay_unless_reinforced",
    "override_fired": null
  }
}
```

Field definitions:

- `why_served` — one line naming the view, the precedence tier, and the
  primary reason (e.g. "tier-1 correction in current workspace").
- `matched_on` — the concrete signals: exact keyword/identifier hits, scope
  match, semantic similarity, freshness window.
- `anchors` — the source cue, rendered from the anchor schema.
- `confidence` — rolled-up trust (certainty × verification state), as
  already exposed by `include_confidence` in recall.
- `support_count` — number of independent supporting entities folded into
  this item (see §3).
- `supersession` — `active | superseded | supersedes:<id> | contested`.
- `override_fired` — names the override when one applied (pin, operator
  directive), else null.

## 3. Evidence-backed ranking (resolves #718)

Serving ranks within precedence tiers by a composite score; evidence
strength is a first-class input, not a side effect:

```
score = relevance(lexical, semantic)
      + evidence_weight(support_count)
      + trust_weight(verified, certainty)
      + freshness_weight(decay, last_reinforced)
      - contradiction_penalty
```

- `support_count` counts **independent** supporters: entities related by
  near-duplicate merge (`deduped`) count once; distinct captures,
  corrections, and observations each count. This prevents one repeated
  artifact from masquerading as broad support.
- A weakly-matching but well-supported claim can outrank a strongly-matching
  one-off within the same tier. Across tiers, precedence (taxonomy §3) is
  absolute: a one-off correction still outranks a broadly-supported belief.
- Every score component appears in `explanation.matched_on` / payload
  fields so ranking is debuggable, not just tunable.

## 4. Relationship to existing recall APIs

| Existing | Relationship |
|---|---|
| `mimir_recall` (fts5/dense/hybrid) | Candidate generator for the `relevant_context` view. Serving adds precedence ordering, explanation payloads, and budgets on top; recall stays the lower-level primitive and is unchanged. |
| `mimir_context` | The `active_instructions` + `relevant_context` views with prompt-budget clamping. `mimir_context` is effectively the first serving consumer; its on-demand mode already implements the relevance gate. |
| `mimir_recall_when` | Trigger-matched subset of `active_instructions`. |
| `mimir_conflicts` | Candidate generator for the `contradictions` view. |
| `mimir_as_of` / `valid_at` / `history` | Time-travel views; served items from these name the temporal mode in `why_served`. |

Design rule: **serving never bypasses the store's invariants** (workspace
isolation, archived exclusion, valid-time). It is an ordering +
explanation layer over the same candidate pools.

## 5. Validation: the briefing flow

Selected synthesis flow (per #722/#726 acceptance): **an operator briefing
on a subsystem, generated from the live shared Vault.**

Worked example — `serve(view="briefing", query="plutus subscription gate
deploy")` against the production shared Vault, 2026-07-21 (real entities,
condensed):

```markdown
## Briefing: plutus subscription gate deploy

### Current state
- Subscription gate SHIPPED 2026-07-20 ~00:50Z; plutus#175 closed by PR #176,
  deployed, gate CLOSED in prod.
  · why: tier-4 recent episodic · anchors: plutus#176 · confidence 0.72
  · support: 3 independent ops memories

### Active instructions
- Deploys are Greg-side only; Cloud has no deploy host access.
  · why: tier-2 scoped instruction (operations) · scope: global
- After any plutus deploy, check stripe_events vs Stripe and replay gaps —
  deploy windows drop webhooks.
  · why: tier-5 synthesized insight · derived from 2 outage episodes

### Corrections in force
- none on this subject

### Open loops
- Flip PLUTUS_SUBSCRIPTIONS_ENABLED=1 when gates #4/#164 close.
- Redeploy main (PRs #172/#173 merged, not yet deployed at time of writing).

### Contradictions / stale
- "Production live v1.1.0; main HEAD 3121d0672d" is SUPERSEDED by the
  2026-07-20 redeploy memory — served here for audit only, excluded from
  current-state section per rule R4.
```

Observations from the validation:

- The precedence model produced the right narrative order with no
  hand-tuning: instructions first, fresh episode as current state, stale
  fact demoted to audit.
- `support_count` behaved as intended: the "gate shipped" claim is backed
  by three independently-written ops memories and correctly outranked the
  higher-similarity but older single-source state note.
- Explanation lines were short enough to inline under each item without
  doubling the prompt size.

## 6. Implementation notes (follow-up engineering)

- Serving endpoint: read-only MCP tool `mimir_serve(view, query, scope,
  budget, explain)`; no new storage.
- Explanation payload is computed from existing fields plus `support_count`
  derivation (taxonomy §5); the only new computed value.
- Budget enforcement mirrors `mimir_context`'s per-model char budget.
- First consumer: Perseus render layer (perseus#833/#835/#838) — the
  cross-repo dependency is explicit by design.

## 7. Acceptance checklist traceability

- #722: view shapes (§1), explanation fields (§2), relationship to recall
  APIs (§4), synthesis flow validated (§5). ✔
- #726: view shapes (§1), explanation metadata (§2), validation flow
  selected and executed (§5). ✔
- #718: explanation payload (§2), support-count-aware ranking (§3),
  prompt-injectable explanations (§2, §5). ✔
