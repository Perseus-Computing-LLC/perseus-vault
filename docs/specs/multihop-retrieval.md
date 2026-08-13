# Multi-hop retrieval arm: graph traversal + entity coverage (#1003)

Status: normative. Surface: `multihop` flag on recall args,
`fused_trace.multihop`.

CogniCore's MultiHopMemoryBackend (v0.5.5+): hop-1 dense+BM25 anchors →
graph traversal (graph_next/graph_prev session adjacency) → final selection
by ENTITY COVERAGE — the set that jointly covers the most entities named in
the query, not the individually highest-scoring chunks (LongMemEval STRICT
R@5 +6.4%). The vault slots this in as a SELECTION STRATEGY over the fused
pool (#883), not a new index.

## Mechanics

1. **Hop expansion** — the top-3 fused anchors' links are followed via the
   existing `graph_expand` (#869 attested-edge gate, workspace scoping, and
   lifecycle gates all apply); neighbors are discounted per hop (0.8^hop,
   hop budget 1) and appended to the pool. Expansion only ADDS — it never
   reorders the anchors themselves.
2. **Coverage selection** — query entities are the deterministic
   stopword-filtered significant tokens (the LongMemEval entity-lexicon
   proxy; no LLM). Greedy set-cover: repeatedly pick the remaining item
   covering the most uncovered query entities, ties by score then id, within
   the caller limit AND the #942 token budget. Once everything is covered
   (or nothing left covers anything uncovered) the walk falls back to plain
   score order.

## Guards

- Opt-in (`multihop`, default OFF) — a default recall stays byte-identical
  (#247).
- Bounded: 3 anchors, 1 hop, 20 neighbors/hop, 12 query entities.
- Deterministic: pool order stable, ties broken by score/id.
- Observable: `fused_trace.multihop` records hop_expanded, expanded_ids,
  selection_order, covered_entities, uncovered_entities.
- Composes: the shared token-budget loop still runs (idempotent — coverage
  selection already respected the same budget); consensus-sources and
  completeness traces are unchanged.

## Evaluation

The #916 harness gains a `multihop` flag: STRICT R@5 at 1/3/5 turns on the
linked-hop corpus (linked entities authored via session_of links), comparing
ranked-walk vs coverage selection at equal token budgets. Target: >= +4%
STRICT R@5 at turn 5 (CogniCore's reported +6.4% bound).
