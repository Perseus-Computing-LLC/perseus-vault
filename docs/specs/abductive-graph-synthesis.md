# Abductive graph synthesis: reflecting over the neighborhood, not just the cluster

Status: design specification
Date: 2026-07-21
Resolves: #740
Related: `synthesis-hypothesis-lifecycle.md` (#739 — what happens to the
hypotheses this produces), `question-conditioned-synthesis.md` (#741 —
question parameter steers the same reflection), `served-memory-api.md`
(explanation payloads for synthesized items),
`memory-taxonomy-and-precedence.md` (insight class, R3: evidence beats
inference)

`mimir_dream` clusters by trigram similarity (threshold 0.3) and reflects
over each cluster **in isolation**. Similarity tells the LLM that episodes
*look alike*; it cannot say *what makes them equivalent*. The transferable
explanation usually depends on context outside the cluster — linked
entities, the surrounding community, the goals in force at the time. This
spec expands the reflection context from cluster-only to
cluster-plus-neighborhood and reframes the reflection question from
induction to abduction, with guardrails against confident thematic
summaries dressed as causal rules.

## 1. Cluster seeding stays similarity-based

Trigram similarity clustering is retained unchanged as the *seeding*
mechanism: it is cheap, deterministic, and good enough to nominate
"episodes worth reflecting over together." Nothing in this spec changes
`similarity_threshold`, `max_clusters`, `max_entities`, or the
evidence-set-hash idempotency key. The change is strictly in what context
accompanies the seeded cluster into reflection.

## 2. Neighborhood expansion of the reflection context

For each seeded cluster, the reflection input is expanded with three
neighborhood sources, all from machinery that already exists:

1. **Linked entities** — one hop of the entity link graph (`mimir_link`
   relationships: `depends_on`, `references`, `evidence_for`, custom) from
   each cluster member.
2. **Community membership** — the GraphRAG community (`mimir_communities`)
   each member belongs to, including its summary; a cluster straddling two
   communities is itself a signal the reflection should see.
3. **Adjacent categories** — entities in sibling categories sharing a
   `topic_path` prefix with cluster members (e.g. episodes plus the
   operations decisions they sit under).

Expansion is bounded by a new depth/width cap: `neighborhood_depth`
(default 1) and `neighborhood_width` (default 10 additional entities per
cluster), charged against the existing `max_entities` budget so total LLM
context cost stays predictable.

Optional: when the caller supplies it, the agent's active goals / recent
journal decisions are appended so reflection can reason about *why*
episodes occurred, not just *that* they co-occur. Absent caller input,
Vault pulls the cluster window's journal `decision` events itself.

## 3. Induction vs. abduction: the reflection framing

The fixed prompt — "what stable pattern do these collectively imply?" — is
induction over a biased sample and tends to produce thematic summaries.
The default reflection question changes to the abductive framing:

> Given these episodes *and their surrounding context*, what underlying
> mechanism or broader system makes them equivalent? State the mechanism,
> what it predicts beyond these episodes, and what would falsify it.

| | Induction (today) | Abduction (this spec) |
|---|---|---|
| Question | What pattern do these imply? | What broader system makes these equivalent? |
| Input | Cluster members only | Cluster + links + community + goals |
| Output shape | Thematic summary | Mechanism + prediction + falsification condition |
| Failure mode | Transfers wrong, confidently | Guarded by §4 |

The required output shape is structural, not cosmetic: an insight that
cannot name a falsification condition is a thematic summary and is written
with reduced certainty (§4).

## 4. Guardrails against confident thematic summaries

A confident thematic summary dressed as a causal rule is worse than no
synthesis, because it transfers wrong. Guardrails:

- **Falsification required.** The insight body MUST carry
  `falsified_by` (what observation would kill it) and `predicts` (at least
  one implication beyond the source episodes). Missing either ⇒ certainty
  capped at the thematic ceiling (config, default 0.4) and the insight is
  flagged `thematic_only: true`.
- **Mechanism must cite context.** A causal claim whose evidence set
  contains *only* cluster members — no neighborhood entity — is treated
  as unsupported mechanism and capped the same way. The whole point of
  neighborhood context is that mechanism lives there.
- **Contradiction behavior unchanged.** Clusters whose sources conflict
  still surface as flagged `contradiction` insights (or fork per
  `synthesis-hypothesis-lifecycle.md` §4); abduction never smooths over
  contradictory evidence.
- **Hypothesis state.** Every abductive insight enters the lifecycle at
  `hypothesis` (per the lifecycle spec); the `predicts` field is what the
  predictive-validation stream tests.

## 5. Evidence typing: `evidence_for` vs. `context`

Neighborhood entities are *not* folded into `evidence_for` — they did not
generate the claim, they situated it. The insight body distinguishes:

```json
{
  "evidence": ["mem-a…", "mem-b…"],
  "context": ["mem-x… (link: depends_on)", "mem-y… (community com-1a2b)"],
  "derivation": "dream",
  "predicts": "next deploy window will also drop webhooks",
  "falsified_by": "a deploy window with zero dropped Stripe webhooks"
}
```

- `evidence` keeps the existing `evidence_for` links and drives the
  evidence-set-hash idempotency key unchanged (context membership does
  not rekey the insight; re-running with a different neighborhood but the
  same cluster still dedupes).
- `context` entities are recorded in the body only, so served explanations
  (`served-memory-api.md` §2) can show *why* the mechanism was inferred.
- Taxonomy rule R3 (direct evidence beats inference) applies as before:
  the insight's precedence is unaffected by how wide its context was.

## 6. Implementation slice

1. Add neighborhood expansion to dream's cluster pipeline (links via
   existing traversal, community via persisted detection), governed by
   `neighborhood_depth`/`neighborhood_width` against `max_entities`.
2. Swap the reflection prompt to the abductive framing with the required
   `predicts`/`falsified_by` output schema; parse and enforce the §4 caps.
3. Record `context` separately from `evidence` in the insight body; keep
   the evidence-set hash over `evidence` only.
4. A/B evaluation on a real vault: neighborhood-conditioned insights vs.
   cluster-only insights, compared on downstream efficacy and predictive
   validation rates (per the lifecycle spec's streams).

## 7. Acceptance checklist traceability

- #740: cluster seeding unchanged, reflection context expanded to links +
  community + adjacent categories (§1–2) ✔; abductive prompt framing
  (§3) ✔; non-cluster neighborhood recorded in the insight with
  `evidence_for`/`context` distinction (§5) ✔; guardrails against
  confident thematic summaries (§4) ✔; A/B acceptance path (§6) ✔.
