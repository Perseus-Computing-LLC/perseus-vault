# Graph-first retrieval for impact, lineage, and dependency questions

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #735
Related: `structured-truth-retrieval-policy.md` (retrieval order, tier 1),
`provenance-classes-derived-facts.md` (evidence links),
`hybrid-retrieval-ranking.md` (score boosts), `served-memory-api.md`
(served explanations), `memory-provenance-and-external-refs.md` (refs)

Some questions are graph questions, not text-search questions: impact,
ownership, lineage, downstream effect. Answering them with keyword search
means broad sweeps over large note/page corpora and hand-reassembled
provenance. Vault already has the primitives — `mimir_link` relationships,
`mimir_traverse`, `mimir_communities` / `mimir_global_recall`, belief
overlay evidence sets. This spec promotes traversal to the *primary*
strategy for these question shapes, defines higher-level helpers, fixes
path rendering, and adds score boosts for linked evidence. It composes
existing tools; it does not add graph storage (strategy doc non-goal).

## 1. Question patterns that prefer traversal

| Question shape | Example | Primary strategy | Why not keyword |
|---|---|---|---|
| Dependency / impact | "what depends on the stripe_events replay job?" | `mimir_traverse` over `depends_on` | keyword finds mentions, not dependents; misses implied links |
| Change propagation | "what changed because of PR #176?" | traverse `derived_from`/`supersedes` from the anchor | freshness filters can't see causality |
| Evidence support | "what evidence supports 'deploy windows drop webhooks'?" | traverse `evidence_for` / belief `supporting_entity_ids` | the claim's text doesn't contain its evidence |
| Decision consequences | "what follows from the decision to gate on #4?" | traverse `implements`/`references`/`depends_on` forward | consequences rarely repeat the decision's keywords |
| Neighborhood / context | "what's around this customer account?" | `mimir_communities`, `mimir_global_recall` | community structure is not a text property |

Heuristic: if the question contains a relational verb (depends, supports,
follows, caused, blocks, supersedes) or names an entity as a *hub*, start
with traversal. Keyword/hybrid recall remains the fallback to *find the
hub entity* when its `(category, key)` isn't known.

## 2. Higher-level helpers

Convenience queries over `mimir_traverse` + the link vocabulary. These are
serving-layer compositions (read-only), not new storage:

| Helper | Definition | Implementation |
|---|---|---|
| `what_depends_on(entity)` | inbound `depends_on`/`implements`/`references`, depth ≤2 | traverse with relationship filter, reversed direction |
| `what_supports(entity)` | outbound `evidence_for` + belief overlay `supporting_entity_ids` + `derived_from` citations | traverse + belief derivation (taxonomy §5) |
| `what_follows_from(decision)` | forward closure over `implements`, `depends_on`, `references` from a decision-class entity | traverse depth ≤3 |
| `what_changed_because_of(anchor)` | entities whose `derived_from`/supersession chain reaches the anchored artifact | traverse + incremental-refresh lineage (§3 there) |
| `lineage_of(entity)` | full `derived_from` → `supersedes` chain back to source artifact | traverse to fixpoint |

Helpers return entities *plus* the relationship path taken (§3); a bare
entity list without paths is a bug, same rule as served explanations.

## 3. Relationship-path rendering

Traversal results render as explicit paths so the agent (and operator) can
see *how* the answer connects:

```
what_follows_from(decision "gate subscriptions on #4"):
  decision/gate-on-4
    ├─ implements → operations/enable-PLUTUS_SUBSCRIPTIONS_ENABLED (open loop)
    ├─ depends_on → operations/stripe-webhook-replay-runbook
    └─ references → pull_request github:Perseus-Computing-LLC/plutus#176
```

Rules:

- Every hop renders `relationship → target`; depth and relationship type
  are always visible. Cycles are broken by first-visit with a `(cycle)`
  marker.
- Path length feeds ranking: shorter paths to the hub outrank longer ones
  within a result set (§4).
- `mimir_context` inlines paths compactly (one line per path, hub-first);
  full trees belong to served views with a budget.

## 4. Score boosts for linked evidence

On lineage/impact questions, linked evidence outranks weak keyword-only
matches:

- **Link-presence boost**: within the composite score (Perseus
  `composite-retrieval-ranking.md`), entities reached via a relationship
  path receive a structural boost proportional to path proximity — a
  direct dependent outranks a depth-3 neighbor, and any path-linked entity
  outranks an unlinked lexical near-match *on lineage questions*.
- **Evidence-density boost**: for `what_supports`-shaped queries,
  `support_count`-bearing entities (observations, beliefs) rank by their
  independent-evidence count, matching served-memory §3.
- These are *boosts within a tier*, not tier changes: taxonomy rules
  R1–R5 and the retrieval-order policy still partition candidates first.
  A path-linked `inference_agent` does not outrank a tier-1 correction.
- Question-shape detection (§1 heuristic) decides whether the boost set
  applies; a plain factual lookup does not get graph boosts.

## 5. Relationship to existing tools

| Existing | Relationship |
|---|---|
| `mimir_traverse` | The primitive; helpers are relationship-filtered, direction-aware compositions of it. Unchanged. |
| `mimir_link` / `mimir_unlink` | The write side; helpers assume the documented relationship vocabulary and tolerate custom types by treating them as `related`. |
| `mimir_communities` / `mimir_global_recall` | Neighborhood questions (§1 last row) and the breadth-first entry when no hub is known. |
| `mimir_recall` | Hub discovery fallback and non-graph questions. Unchanged. |
| belief overlay | `what_supports` reads its `supporting_entity_ids`; no new derivation. |

## 6. Success criteria mapping (#735)

- Better impact/dependency answers: traversal is primary for the §1
  patterns, not a fallback after failed search.
- Less broad search: helpers answer from the link graph directly; keyword
  is used only to find hubs.
- Explicit provenance chains: every helper result carries rendered paths
  (§3), satisfying the served-memory explanation contract.

## 7. Implementation slice

- Serving-layer helper queries (`what_depends_on`, `what_supports`,
  `what_follows_from`, `what_changed_because_of`, `lineage_of`) as
  compositions over `mimir_traverse` + belief derivation; read-only.
- Path rendering per §3 in served views and `mimir_context`.
- Graph boost signals into the composite score behind question-shape
  detection (§4); golden-vector tests: direct dependent beats depth-3
  neighbor; path-linked beats unlinked lexical match on lineage queries.
