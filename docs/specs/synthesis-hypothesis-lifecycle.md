# Synthesis output as hypotheses: lifecycle, validation, and revision

Status: design specification
Date: 2026-07-21
Resolves: #739 · Consumed by: Perseus-Computing-LLC/perseus#846
Related: `abductive-graph-synthesis.md` (#740 — better hypotheses need wider
context), `question-conditioned-synthesis.md` (#741 — persistence rule for
reflective output), `memory-taxonomy-and-precedence.md` (insight class,
precedence R3), `served-memory-api.md` (explanation payload, serving views)

`mimir_dream` writes durable semantic insights and treats them as settled
explanations: the only post-write feedback is `mimir_follow` efficacy, and
the only referee for a wrong insight is decay or manual supersession. This
spec recasts synthesis output as *hypotheses* with an explicit lifecycle,
two typed evidence streams updating certainty, and a revision/split
operator for hypotheses that fail validation.

## 1. Lifecycle state on synthesized insights

Every entity with `derivation='dream'` (or any synthesized insight) carries
a `lifecycle` field in its body. States flow `hypothesis → validating →
durable`, with `contradicted → split` terminal branches (split children
restart at `hypothesis`):

| State | Meaning | Serving behavior |
|---|---|---|
| `hypothesis` | Freshly synthesized; no validation yet | Served as insight (tier 5) but *named* as hypothesis in explanations |
| `validating` | ≥1 predictive event recorded; not conclusive | Same; explanation carries the running validation tally |
| `durable` | Crossed the durability threshold (§3) | Served as a normal insight |
| `contradicted` | Predictive failure recorded; awaiting revision | Excluded from default serving (R4); appears in contradictions view |
| `split` | Superseded by ≥2 child hypotheses (§4) | History/audit only, via `as_of`/`valid_at` |

Transitions are journaled as `observation` events linked to the insight
(reconstructable via `mimir_timeline`). Initial state is `hypothesis`;
`mimir_consolidate` observations (mechanical merges, no causal claim) are
exempt.

## 2. Usefulness vs. predictive validity: two evidence streams

The discussion's core distinction is made explicit in the data model:

- **Usefulness** — "was the recalled insight followed?" Captured in-band by
  `mimir_follow`. Continuous, cheap, *weak*: a heuristic can be useful
  without capturing the mechanism, and works until the environment shifts.
- **Predictive validity** — "did the insight correctly predict what happened
  next?" Observable only when the environment produces the test case:
  sparse, decisive, *strong*. Captured out-of-band by the orchestration
  layer (§5).

These are different measurements and MUST NOT be conflated into one
counter. `followed/missed` continues to feed efficacy status and decay
resistance (unchanged); predictive events move certainty and lifecycle.

## 3. Certainty as a prior updated by both streams

Dream's blended certainty (LLM confidence × evidence coverage) becomes the
*prior*. Updates are typed and asymmetric:

```
certainty ← prior
  + usefulness updates   (small, capped: ±Δu per event, e.g. 0.02)
  + validation updates   (large: confirmed +Δv, contradicted −3·Δv, e.g. Δv = 0.15)
```

- A single predictive contradiction outweighs many usefulness misses; the
  ratio is config, not hard-coded.
- Transitions are threshold-driven: `validating` on first predictive event;
  `durable` at certainty ≥ durability threshold with ≥1 confirmation and no
  open contradiction; `contradicted` on any event dropping certainty below
  the durability floor.
- `mimir_follow` never promotes a hypothesis to `durable` on its own — pure
  usefulness cannot validate a causal claim. This is the guard against
  heuristics that work until they silently don't.

## 4. Revision and split operator (generalizing the contradiction path)

When a hypothesis is contradicted, decay is no longer the only referee:

```
mimir_revise(insight_id, children: [{claim, evidence_ids, certainty_hint}],
             reason) -> { parent: bounded, children: [insight_id, ...] }
```

- The parent is **superseded, never deleted**: `mimir_supersede` bounds its
  `valid_to` (bitemporal chain intact for audit), and it moves to the
  `split` terminal state.
- Each child is a new insight with `derived_from` = parent + cited evidence,
  `lifecycle: hypothesis`, certainty re-prior'd from its own evidence — the
  fork restarts validation rather than inheriting the parent's failure.
- This generalizes dream's contradiction-insight behavior from "flag the
  tension" to "fork the hypothesis": a contradiction cluster MAY now emit
  ≥2 competing insights, each linked to the others as competitors.
- Competing live hypotheses are *both* served to the contradictions view
  (taxonomy R5) until validation picks a winner; certainty, not decay,
  resolves the contest.

## 5. Predictive-validation event intake (Perseus #846 hook)

The vault cannot observe the environment; the orchestration layer can.
Perseus #846 emits typed validation events when a recalled insight's claim
is later confirmed or contradicted by real outcomes (task results, error
recurrence, user corrections). Vault's side of the contract:

```
mimir_validate(insight_id, outcome: 'confirmed' | 'contradicted',
               context: string, session_id?, source: 'perseus' | 'operator')
```

- Accepts the event, records it as a journal `observation` linked to the
  insight (attributable: insight id, session, outcome description), applies
  the §3 certainty update, and drives the §1 lifecycle transition.
- Events are opportunistic, never polled — Vault stores whatever arrives
  and never schedules its own counterfactual tests.
- User corrections via `mimir_correct` that reference an insight's subject
  SHOULD also emit a `contradicted` event; corrections are the
  highest-quality falsification source already flowing.
- Idempotent by (insight_id, session_id, outcome) so orchestration retries
  cannot double-count.

## 6. Surfacing in recall and serving

- Recall results on synthesized insights include `lifecycle` and the
  validation tally (`confirmed: n, contradicted: m`).
- Serving explanations (`served-memory-api.md` §2) name the state in
  `why_served` — e.g. "tier-5 insight (hypothesis, 1 confirmation)" — so
  consumers know how much to trust the claim; split pairs join the
  `contradictions` view.

## 7. Implementation slice

1. Add `lifecycle` + validation tally to dream-insight bodies; default
   existing dream insights to `durable` (grandfathered, no event history).
2. Implement `mimir_validate` (journal write + certainty update + state
   transition); wire `mimir_correct` to emit on insight-referencing
   corrections.
3. Implement `mimir_revise` on top of `mimir_supersede` + `mimir_remember`;
   children carry `derived_from` back-links.
4. Surface `lifecycle` in recall/serving explanation payloads.

No schema migration: lifecycle lives in body_json; certainty updates reuse
the existing field; journal events provide the audit trail.

## 8. Acceptance checklist traceability

- #739: lifecycle state visible in recall (§1, §6) ✔; usefulness vs.
  predictive validity with asymmetric updates (§2, §3) ✔; split/revision
  operator with bounded parent valid-time and ≥2 `derived_from` children
  (§4) ✔; perseus#846 consumer contract for the validation event (§5) ✔.
