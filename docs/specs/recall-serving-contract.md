# Recall Serving Contract — coverage, budget, and bounded priors (#953 #954 #956)

The recall path's *serving* contract: what a recall response means, how much
context it may inject, and how priors may move the ranking. Codifies the
abstention contract (#953), token-budget truncation + depth-sweep methodology
(#954), and derived max-overturn prior bounds (#956).

## 1. Coverage descriptor — the outcome block (#953)

Every recall/ask response can carry an `outcome` block
(`RecallOutcome`, #864/#873/#887). It distinguishes **"nothing found"** from
**"couldn't look"** and is the machine-readable abstention surface.

| `status` | Meaning | `abstained` | Typical `reason` |
|---|---|---|---|
| `fresh` | Full semantic+sparse service; results delivered | false | — |
| `partial` | Results delivered but an arm was degraded (semantic backend didn't serve a query embedding; embeds pending) | false | `partial_arms`, `pending_embeds` |
| `timeout` | Caller deadline exceeded; set may be incomplete (#864) | true | `deadline_elapsed` |
| `unavailable` | Fatal: DB unhealthy or a recall error — never a silent empty | true | `db_unhealthy`, `recall_error:<err>` |
| `empty` | Store healthy, query executed, zero hits — genuine no-evidence | true | `no_match`, `empty_store`, `index_behind` |
| `stale` | Semantic path requested but the vector index is behind/not serving | true | `semantic_backend_not_serving`, `index_behind` |

Maps onto plugin-side statuses (fresh/partial/timeout/unavailable/empty/stale)
so callers can branch uniformly across providers.

**Abstention contract.** The retrieval layer never *decides* "no answer": it
returns the best available evidence plus this descriptor, and the *model*
decides. Evidence-required callers (verify-before-answer, grounded answering)
MUST abstain — never best-guess — when `status` is `unavailable`, `stale`, or
`empty` with `abstained: true` and the retrieved evidence does not answer the
query. The vault itself never fabricates: it has no answer-generation path
that runs without retrieved evidence.

The response wire is versioned as `perseus-vault-recall-wire/v1`. A conforming
response contains `items` and `total`, and a non-empty response contains the
`retrieval_profile` that served it. Each item keeps the server's order and
may contain:

| Field | Contract |
|---|---|
| `wire_rank` | Adapter-derived one-based position copied from the response order; authoritative unless a separately named reranking stage is requested. |
| `score` | Optional semantic relevance score. Missing/null means unavailable, not zero. |
| `score_semantics` | Present only with `score`; identifies the score definition. |
| `decay_score` | Freshness/lifecycle signal used for decay and retention; never a substitute for semantic relevance. |
| `why_served` | Optional explanation of serving/membership; it does not change rank. |

Adapters validate the envelope, preserve membership/order, and reject unknown or
malformed shapes as `unavailable`; they must not turn a malformed response into
an empty successful result. The shared fixture is
`benchmark/package/recall_wire_fixture.json`, and the client/benchmark helpers
implement the same validation boundary.

**Score gates are non-functional.** Retrieval-side score thresholds do not
discriminate: embedding scores saturate near 1.0 for any well-formed sentence
(multilingual embeddings place every well-formed sentence in the same
neighborhood; a min-scores sweep 0.2→0.65 produces byte-identical results).
Consequences:

- `min_decay` is **deprecated**: it remains an API-compatible filter on
  `decay_score`, but decay scores saturate for fresh entities and it cannot
  separate evidence from noise. Do not rely on it; no new score gates may be
  added. (`RecallParams.min_decay`, `RecallArgs.min_decay`.)
- No retrieval-side threshold may gate an answer. Suppression is governed by
  visibility/erasure state, never by score.

## 2. Token-budget truncation + depth-sweep methodology (#954)

**Budget contract** (fused serving path, #883): recall honors a token budget
with deterministic whole-item truncation.

- Estimator: `tokens ≈ body_json.len() / 4` (chars/4), the documented #883
  estimate — deterministic, no model needed.
- Packing: items are delivered in rank order; an item is kept only if it fits
  the remaining budget; **min-1 semantics** — the top-ranked entity is always
  delivered even when it alone exceeds the budget.
- `max_tokens` (default 4096) sets the budget; `depth_budget`
  `low|mid|high` → 1024/4096/16384 when `max_tokens` is unset; `max_tokens: 0`
  derives from `depth_budget`. The truncation trace (`FusedTruncationTrace`)
  records `budget_tokens`, `estimated_tokens_used`, `retained`, `dropped`.
- Budget monotonicity is a test invariant: increasing the budget never loses
  delivered hits (superset property).

**Non-monotonicity is a fact, not a bug.** More context does not reliably mean
better ranking. Published k-sweep evidence (Hindsight, 2026-08-11, same query
set): recall 0.500→0.694 while tokens 448→5,488; k=48 scored *worse* than
k=32 while costing ~1,000 more tokens; k=96+ saturates (k=128 buys 399 tokens
and zero recall). Recall@k measures presence in context, not model usage —
past saturation, the extra tokens dilute attention.

**Operating point is a measurement, not a convention.** Operators pick their
depth/budget per deployment from a depth-sweep curve, not from "deeper =
better". Ship the harness:

- `benchmark/recall/depth_sweep.py` — for `k`/budget in the sweep set
  {8, 16, 24, 32, 48, 64, 96, 128} (and token budgets across the same
  range), measure recall@k and injected tokens over the fixed offline
  dataset; emit `depth_sweep_report.json` with the curve and print the
  operating-point table. Offline, deterministic, no API key — same
  reproducibility bar as `benchmark/recall/run.py`.

## 3. Bounded priors — derived max-overturn exponents (#956)

Priors (recency, content witness, provenance trust) are bounded so they can
never outvote query relevance beyond an explicit constant. Borrowed from
NexusMem (`src/retrieval/rank.ts`, MIT; their observed failure — a 44% signal
edge overturning a better match on a 15% relevance deficit — is the same class
the hybrid/provenance ranker guards against).

**Formula.** Each prior contributes a factor in `[floor, 1]` (high signal →
factor ≈ 1, no penalty; no signal → factor = floor, maximum penalty), raised
to a **derived** exponent:

```
factor_p = floor_p + (1 - floor_p) * signal_p        # signal_p ∈ [0, 1]
score    = relevance × ∏ factor_p^e_p
e_p      = ln(MAX_PRIOR_OVERTURN) / ln(1 / floor_p)  # span^e = MAX_PRIOR_OVERTURN
```

With `floor_p^e_p = 1 / MAX_PRIOR_OVERTURN`, a single prior can overturn at
most a `MAX_PRIOR_OVERTURN`× (default **2.0×**) relevance gap — never zero a
strong match, never crown a weak one. Monotonic: priors still order
equally-relevant hits exactly as before.

**Floors** (NexusMem-derived, documented constants):

| Prior | Floor | Exponent @ 2.0 |
|---|---|---|
| recency (half-life decay) | 0.3 | ≈0.576 |
| content witness (substring match) | 0.2 | ≈0.431 |
| provenance trust (verified/certainty) | 0.2 | ≈0.431 |

**Application** (hybrid + fused serving paths):

- *Recency*: the RRF score multiplier `0.5^(age/hl)` is floored at 0.3 and
  exponentiated. Previously unbounded (a 100-year-old memory → ≈0).
- *Content witness* (`content_weight`) and *provenance trust* (`trust_weight`):
  converted from additive boosts (which could swamp small RRF scores and
  saturate decay_score at 1.0 — a contributor to the #953 saturation class)
  to floored multiplicative factors. Signal = the existing
  weight × damped-strength products, clamped to [0, 1].
- *Arm weights* (sparse/graph/strategy multipliers) are **fusion-level**
  priors, not entity-level: each arm's contribution is bounded by the RRF
  math (`1/(k+rank)`), and the multipliers are documented tuning constants.
  The entity-level bound here does not subsume them.

**Configuration.** `max_prior_overturn` on `RecallParams`/`RecallArgs`
(default **2.0**; ≤ 0 selects the legacy additive/unbounded path for exact
backward compatibility). Derived exponents are pure functions of the floor
and the constant — `prior_exponent(floor, max_overturn)` — unit-tested to
`span^e == max_overturn`.

**Residual hole (documented).** Caps are per-prior, not joint: a hit that is
both fresh and high-signal can jointly overturn up to
`2^3 = 8×` worst case. Accepted for v1 (same as the upstream borrow).

## 4. Verified invariants

- Out-of-scope query over a populated store: 0 hits, `outcome.status == empty`,
  `abstained == true`, reason `no_match` — the abstention bar (no invented
  facts; the model receives "no evidence", never a fabricated answer).
- Fatal backend failure: `unavailable`, never a silent empty (#953).
- Budget monotonicity: budget B ⊇ budget B/2 delivered sets (#954).
- Overturn bound: with a 2× relevance gap, no single prior flips the winner at
  `max_prior_overturn = 2.0`; a large constant (e.g. 64) allows the flip (#956).
- Legacy mode: `max_prior_overturn ≤ 0` reproduces the additive/unbounded
  ordering bit-for-bit on the prior tests.
