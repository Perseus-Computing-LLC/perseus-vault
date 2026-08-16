# Intent-aware typed-relational traversal

Status: **Implemented** (2026-08-16, #1065). MAGMA pattern
(arXiv:2601.03236): relation-specific retrieval views with query-intent
driven traversal, returning the selected path plus rejected distractors.

## 1. Relation views

| View | Relations traversed | Policy |
|---|---|---|
| `temporal` | valid-time ordering | serve hits in recency order; candidates more than a year behind the anchor are rejected with `outside_valid_time_window` |
| `causal` | `depends_on` / `causes` / `updates` / `invalidates` / `derived_from` edges | expand only the top hit, one hop; wrong-kind edge targets become named distractors |
| `entity` | `mentions` / identity neighborhood | all outbound links of the top-3 anchors; deeper base hits rejected |
| `semantic` | `semantic_similar` ranking | the ranked list IS the policy; the tail beyond `limit` is rejected |

Routing is deterministic and LLM-free: `route_intent_to_view` maps the
existing graph-utility classification (multi-hop/relational → causal,
temporal → temporal, entity-centric → entity, else semantic). Identical
query → identical route, so runs are reproducible.

## 2. Explainable paths + rejected distractors

`perseus_vault_typed_traversal` returns:

- `path` — steps carrying the relation each was taken over (`via` = the
  entity it was reached from);
- `rejected` — every distractor the policy dropped, with the reason;
- `tokens_selected` / `tokens_rejected` — per-run token accounting for the
  context-budget discipline.

The selected path is explainable in retrieval telemetry by construction;
the rejected half makes the policy auditable.

## 3. Ablation reporting

`perseus_vault_traversal_ablation` reports per-view means (runs, mean
selected/rejected tokens, distractor ratio) over recorded `traversal_runs`
(schema v53) — the standing answer to "is each view earning its token
cost?".

## 4. Benchmark status

The full external equal-token-budget benchmark (LongMemEval-style,
#916/#954 methodology) is gated on the benchmark track (#1021/#916) and
operator spend authorization; the ablation substrate and per-run token
accounting here are what those runs will consume. The internal regression
suite proves routing determinism, per-view traversal behavior, explainable
paths, and ablation accumulation.
